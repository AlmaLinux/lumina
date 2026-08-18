"""Named cohorts: the whole statistics page, recomputed inside a subset of machines.

"Arm servers", "machines with a discrete GPU", "dual socket": a question about part of
the fleet rather than all of it.

The property that makes a segment worth having is that it is **its own rollup**, not a
filter over published percentages. The criteria narrow the submissions first, dedup runs
inside the narrowed set, and every dimension is recounted, so a cohort's shares add up to
100% of that cohort. Slicing the whole-fleet numbers instead would give a share of a
share, which answers nothing.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse

from lumina.survey import services, stats
from lumina.survey.models import SurveySegment, SurveyStat, SurveySubmission

pytestmark = pytest.mark.django_db


def _sub(*, when=None, **kw):
    defaults = dict(
        origin=SurveySubmission.ORIGIN_SURVEY,
        trust_tier=SurveySubmission.TIER_VERIFIED,
    )
    defaults.update(kw)
    sub = SurveySubmission.objects.create(**defaults)
    if when:
        SurveySubmission.objects.filter(pk=sub.pk).update(received_at=when)
    return sub


def _at(year, month, day=15):
    return dt.datetime(year, month, day, 12, tzinfo=dt.UTC)


def _arm_segment(**kw):
    defaults = dict(
        name="Arm servers", slug="arm", description="Machines on aarch64.",
        criteria=[{"field": "arch", "op": "eq", "value": "aarch64"}],
    )
    defaults.update(kw)
    return SurveySegment.objects.create(**defaults)


def _mixed_fleet():
    """Three Arm machines and one x86, all reporting in September."""
    for i in range(3):
        _sub(when=_at(2026, 9), identity_hash=f"arm{i}", arch="aarch64",
             cpu_vendor="Ampere", gpu_vendor="ASPEED")
    _sub(when=_at(2026, 9), identity_hash="x86", arch="x86_64",
         cpu_vendor="GenuineIntel", gpu_vendor="NVIDIA")


# --- the rollup ------------------------------------------------------------------

def test_a_segment_gets_its_own_rollup_rows():
    _arm_segment()
    _mixed_fleet()

    services.rebuild_survey_stats()

    assert SurveyStat.objects.filter(segment="arm").exists()
    assert SurveyStat.objects.filter(segment="").exists(), "the whole fleet is still there"


def test_a_cohort_share_is_of_the_cohort_not_a_slice_of_the_fleet():
    _arm_segment()
    _mixed_fleet()
    services.rebuild_survey_stats()

    fleet = next(s for s in stats.distribution("2026-09")
                 if s["dimension"] == "cpu_vendor")
    arm = next(s for s in stats.distribution("2026-09", segment="arm")
               if s["dimension"] == "cpu_vendor")

    # Ampere is three of four machines overall, and all three of three in the cohort.
    assert round(next(b.share for b in fleet.get("buckets") if b.label == "Ampere"), 1) == 75.0
    assert round(next(b.share for b in arm["buckets"] if b.label == "Ampere"), 1) == 100.0


def test_the_machine_count_is_the_cohorts_own():
    _arm_segment()
    _mixed_fleet()
    services.rebuild_survey_stats()

    assert stats.machine_count("2026-09") == 4
    assert stats.machine_count("2026-09", segment="arm") == 3


def test_dedup_happens_inside_the_cohort():
    # One machine, two reports in the month: one machine in the cohort, not two.
    _arm_segment()
    _sub(when=_at(2026, 9, 2), identity_hash="same", arch="aarch64", cpu_vendor="Ampere")
    _sub(when=_at(2026, 9, 20), identity_hash="same", arch="aarch64", cpu_vendor="Ampere")
    services.rebuild_survey_stats()

    assert stats.machine_count("2026-09", segment="arm") == 1


def test_a_disabled_segment_is_still_rolled_up():
    """So switching one back on does not leave a hole in its history."""
    _arm_segment(enabled=False)
    _mixed_fleet()

    services.rebuild_survey_stats()

    assert SurveyStat.objects.filter(segment="arm").exists()


def test_rebuilding_replaces_a_cohort_rather_than_doubling_it():
    _arm_segment()
    _mixed_fleet()
    services.rebuild_survey_stats()
    services.rebuild_survey_stats()

    assert SurveyStat.objects.filter(
        segment="arm", period="2026-09", dimension="cpu_vendor", bucket="Ampere",
        tier_scope="verified",
    ).count() == 1


# --- the criteria are validated where they are typed -----------------------------

def test_a_field_outside_the_allowlist_is_refused():
    # Not a typo guard: a segment on submitter__* would make a public page that says
    # something about one person's machines.
    segment = SurveySegment(
        name="Sneaky", slug="sneaky",
        criteria=[{"field": "submitter__username", "op": "eq", "value": "someone"}],
    )

    with pytest.raises(ValidationError):
        segment.full_clean()


def test_an_unknown_operator_is_refused():
    segment = SurveySegment(
        name="Bad op", slug="bad-op",
        criteria=[{"field": "arch", "op": "regex", "value": ".*"}],
    )

    with pytest.raises(ValidationError):
        segment.full_clean()


def test_empty_criteria_are_refused():
    with pytest.raises(ValidationError):
        SurveySegment(name="Everything", slug="everything", criteria=[]).full_clean()


def test_in_needs_a_list():
    segment = SurveySegment(
        name="Vendors", slug="vendors",
        criteria=[{"field": "cpu_vendor", "op": "in", "value": "Ampere"}],
    )

    with pytest.raises(ValidationError):
        segment.full_clean()


def test_a_valid_segment_passes_validation():
    _arm_segment().full_clean()


# --- the operators --------------------------------------------------------------

def test_clauses_are_combined_with_and():
    _sub(identity_hash="a", arch="aarch64", cpu_sockets=2)
    _sub(identity_hash="b", arch="aarch64", cpu_sockets=1)
    _sub(identity_hash="c", arch="x86_64", cpu_sockets=2)
    segment = SurveySegment.objects.create(
        name="Dual-socket Arm", slug="dual-arm",
        criteria=[
            {"field": "arch", "op": "eq", "value": "aarch64"},
            {"field": "cpu_sockets", "op": "gte", "value": 2},
        ],
    )

    kept = {s.identity_hash for s in segment.narrow(SurveySubmission.objects.all())}

    assert kept == {"a"}


def test_not_blank_selects_machines_that_reported_the_facet():
    _sub(identity_hash="has", gpu_vendor="NVIDIA")
    _sub(identity_hash="none", gpu_vendor="")
    segment = SurveySegment.objects.create(
        name="With a GPU", slug="gpu",
        criteria=[{"field": "gpu_vendor", "op": "not_blank"}],
    )

    kept = {s.identity_hash for s in segment.narrow(SurveySubmission.objects.all())}

    assert kept == {"has"}


def test_ne_excludes():
    _sub(identity_hash="aspeed", gpu_vendor="ASPEED")
    _sub(identity_hash="nvidia", gpu_vendor="NVIDIA")
    segment = SurveySegment.objects.create(
        name="Discrete graphics", slug="discrete",
        criteria=[{"field": "gpu_vendor", "op": "ne", "value": "ASPEED"}],
    )

    kept = {s.identity_hash for s in segment.narrow(SurveySubmission.objects.all())}

    assert kept == {"nvidia"}


# --- the page -------------------------------------------------------------------

def test_the_page_offers_the_segment_and_scopes_to_it(client):
    _arm_segment()
    _mixed_fleet()
    services.rebuild_survey_stats()

    response = client.get(reverse("results:stats"), {"segment": "arm"})
    body = response.content.decode()

    assert response.context["segment"].slug == "arm"
    assert response.context["machine_total"] == 3
    assert "Arm servers" in body
    assert "Machines on aarch64." in body


def test_the_picker_lists_enabled_segments_only(client):
    _arm_segment()
    _arm_segment(name="Retired", slug="retired", enabled=False)
    _mixed_fleet()
    services.rebuild_survey_stats()

    body = client.get(reverse("results:stats")).content.decode()

    assert "Arm servers" in body
    assert "Retired" not in body


def test_an_unknown_segment_falls_back_to_the_whole_fleet(client):
    """A stale link, or a segment an admin has since deleted, still lands on a page."""
    _mixed_fleet()
    services.rebuild_survey_stats()

    response = client.get(reverse("results:stats"), {"segment": "no-such-thing"})

    assert response.status_code == 200
    assert response.context["segment"] is None
    assert response.context["machine_total"] == 4


def test_a_cohort_with_no_machines_says_so_rather_than_looking_broken(client):
    SurveySegment.objects.create(
        name="RISC-V", slug="riscv",
        criteria=[{"field": "arch", "op": "eq", "value": "riscv64"}],
    )
    _mixed_fleet()
    services.rebuild_survey_stats()

    body = client.get(reverse("results:stats"), {"segment": "riscv"}).content.decode()

    assert "No machines in this set yet" in body


def test_periods_offered_are_the_cohorts_own(client):
    # The cohort only exists in September; August must not be offered for it, or the
    # picker leads to a page that is empty for no visible reason.
    _arm_segment()
    _sub(when=_at(2026, 8), identity_hash="x86-aug", arch="x86_64")
    _mixed_fleet()
    services.rebuild_survey_stats()

    assert "2026-08" in stats.available_periods()["month"]
    assert "2026-08" not in stats.available_periods("arm")["month"]
