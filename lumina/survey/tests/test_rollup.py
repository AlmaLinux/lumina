"""The rollup dedups machines, scopes by tier, excludes VMs, and never suppresses.

Note the asymmetry in these tests: submissions carry what a machine actually reports
("AuthenticAMD", the CPUID string), and the published bucket is the brand ("AMD"). The
rollup maps it through the catalog's own table, so a machine is not Intel in the catalog
and GenuineIntel in the statistics. See lumina.vendors.cpu_aliases.
"""
from __future__ import annotations

import pytest
from django.core.management import call_command
from django.utils import timezone

from lumina.survey import services
from lumina.survey.models import SurveyStat, SurveySubmission

pytestmark = pytest.mark.django_db


def _sub(**kw) -> SurveySubmission:
    defaults = dict(
        origin=SurveySubmission.ORIGIN_SURVEY,
        trust_tier=SurveySubmission.TIER_VERIFIED,
    )
    defaults.update(kw)
    return SurveySubmission.objects.create(**defaults)


def _this_year() -> str:
    return str(timezone.now().year)


def _stat(dimension, bucket, tier_scope="all", period=None):
    """One rollup row. Scoped to a period because the rollup emits two granularities.

    Defaults to the year: these tests are about what the annual rollup counts. The
    monthly rows, and the rule that the two are never summed, are covered in
    test_stats_periods.py.
    """
    return SurveyStat.objects.get(
        dimension=dimension, bucket=bucket, tier_scope=tier_scope,
        period=period or _this_year(),
    )


def test_counts_distinct_machines_by_dimension():
    _sub(identity_hash="aaa", cpu_vendor="AuthenticAMD", arch="x86_64")
    _sub(identity_hash="bbb", cpu_vendor="AuthenticAMD", arch="x86_64")
    _sub(identity_hash="ccc", cpu_vendor="GenuineIntel", arch="x86_64")

    services.rebuild_survey_stats()

    assert _stat("cpu_vendor", "AMD").count == 2
    assert _stat("arch", "x86_64").count == 3


def test_dedups_same_machine_keeping_the_most_recent():
    _sub(identity_hash="dup", cpu_vendor="AuthenticAMD")
    _sub(identity_hash="dup", cpu_vendor="GenuineIntel")  # same machine, submitted later

    services.rebuild_survey_stats()

    assert _stat("cpu_vendor", "Intel").count == 1
    assert not SurveyStat.objects.filter(dimension="cpu_vendor", bucket="AMD").exists()


def test_excludes_vms_and_dismissed():
    _sub(identity_hash="phys", cpu_vendor="AuthenticAMD")
    _sub(identity_hash="vm", cpu_vendor="AuthenticAMD", virtual=True)
    dismissed = _sub(identity_hash="dis", cpu_vendor="AuthenticAMD")
    dismissed.review_state = SurveySubmission.REVIEW_DISMISSED
    dismissed.save(update_fields=["review_state"])

    services.rebuild_survey_stats()

    assert _stat("cpu_vendor", "AMD").count == 1


def test_verified_scope_excludes_community():
    _sub(identity_hash="v", cpu_vendor="AuthenticAMD", trust_tier=SurveySubmission.TIER_VERIFIED)
    _sub(identity_hash="c", cpu_vendor="AuthenticAMD", trust_tier=SurveySubmission.TIER_COMMUNITY)

    services.rebuild_survey_stats()

    assert _stat("cpu_vendor", "AMD", "all").count == 2
    assert _stat("cpu_vendor", "AMD", "verified").count == 1


def test_no_small_bucket_suppression():
    _sub(identity_hash="rare", cpu_model="Exotic CPU 9999")
    services.rebuild_survey_stats()
    assert _stat("cpu_model", "Exotic CPU 9999").count == 1  # a single machine still counts


def test_rollup_is_idempotent():
    _sub(identity_hash="x", cpu_vendor="AuthenticAMD")
    services.rebuild_survey_stats()
    services.rebuild_survey_stats()
    assert SurveyStat.objects.filter(dimension="cpu_vendor", bucket="AMD",
                                     tier_scope="all", period=_this_year()).count() == 1


def test_management_command_rebuilds():
    _sub(identity_hash="x", cpu_vendor="AuthenticAMD")
    call_command("survey_rollup")
    assert SurveyStat.objects.exists()


# --- the hourly refresh -----------------------------------------------------------
#
# Two timers run this command: a nightly full rebuild, and an hourly ``--current`` that
# touches only the month submissions are still arriving in. The month is worked out here
# rather than by ``date`` in the unit file, because Django decides which period a
# submission falls in using the project's timezone.

def test_current_rebuilds_only_this_month():
    import datetime as dt

    from lumina.survey.models import SurveySubmission

    old = _sub(identity_hash="old", cpu_vendor="AuthenticAMD")
    SurveySubmission.objects.filter(pk=old.pk).update(
        received_at=dt.datetime(2024, 1, 15, 12, tzinfo=dt.UTC)
    )
    _sub(identity_hash="new", cpu_vendor="GenuineIntel")
    services.rebuild_survey_stats()
    SurveyStat.objects.filter(period="2024-01").delete()   # a stale period, left behind

    call_command("survey_rollup", "--current")

    assert not SurveyStat.objects.filter(period="2024-01").exists(), "untouched"
    assert SurveyStat.objects.filter(
        period=timezone.localtime().strftime("%Y-%m")
    ).exists()


def test_current_uses_the_projects_timezone_not_utc():
    from lumina.survey.management.commands.survey_rollup import current_month

    assert current_month() == timezone.localtime().strftime("%Y-%m")


def test_current_and_period_are_mutually_exclusive():
    from django.core.management.base import CommandError

    with pytest.raises(CommandError):
        call_command("survey_rollup", "--current", "--period", "2026")
