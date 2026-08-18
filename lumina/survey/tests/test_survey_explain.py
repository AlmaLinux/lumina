"""Why a machine is or is not in the published statistics, explained.

A machine can be missing for half a dozen unrelated reasons and every one of them looks
identical from the statistics page: an absence. The submission's review page made it
worse, listing a card the rollup had skipped, because the page shows what the machine
reported and the rollup counts what qualifies. This is the account that closes that gap.

Asserted on the structured result rather than on rendered text, so the wording can be
improved without breaking the tests, with a few checks that each surface actually renders
it. One reasoning path, two renderers: the reviewer's page and the management command.
"""
from __future__ import annotations

from io import StringIO

import pytest
from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.urls import reverse

from lumina.hardware.models import ComponentExclusionRule
from lumina.survey import services
from lumina.survey.explain import explain
from lumina.survey.models import SurveyStat, SurveySubmission

pytestmark = pytest.mark.django_db

_NVIDIA = {
    "pci": "01:00.0", "class_id": "0300",
    "pci_ids": {"vendor": "NVIDIA Corporation [10de]",
                "device": "GA102 [GeForce RTX 3090] [2204]"},
    "driver": "nvidia",
}
_ASPEED = {
    "pci": "07:00.0", "class_id": "0300",
    "pci_ids": {"vendor": "ASPEED Technology, Inc. [1a03]",
                "device": "ASPEED Graphics Family [2000]"},
    "driver": "ast",
}


def _sub(*, gpus=(_NVIDIA,), **kw):
    defaults = dict(
        origin=SurveySubmission.ORIGIN_SURVEY,
        trust_tier=SurveySubmission.TIER_VERIFIED,
        identity_hash="abcdef123456", cpu_vendor="GenuineIntel",
        inventory={"summary": {"pci_devices": list(gpus), "gpus": []}},
    )
    defaults.update(kw)
    return SurveySubmission.objects.create(**defaults)


def _failed(report) -> list[str]:
    return [check.label for check in report.checks if not check.ok]


# --- each reason it can name -----------------------------------------------------

def test_a_counted_machine_reports_every_gate_passed():
    sub = _sub()
    services.rebuild_survey_stats()

    report = explain(sub)

    assert _failed(report) == []
    assert report.gpus == ["NVIDIA"]
    assert report.cpu == "Intel"
    assert report.verdict_ok is True


def test_a_dismissal_is_named():
    sub = _sub()
    services.moderate_submission(sub, by=None, dismiss=True)

    report = explain(sub)

    assert _failed(report) == ["Moderation"]
    assert report.counts is False
    assert "counts nowhere" in report.verdict or "contributes nothing" in report.verdict


def test_a_virtual_machine_is_named():
    report = explain(_sub(virtual=True, virt_kind="kvm"))

    assert _failed(report) == ["Bare metal"]
    assert "kvm" in next(c.detail for c in report.checks if c.label == "Bare metal")


def test_an_exclusion_rule_is_named_with_its_reason():
    ComponentExclusionRule.objects.create(
        vendor_id="10de", device_id="2204", kind="gpu", reason="excluded by a reviewer",
    )

    report = explain(_sub())

    assert [d.counted for d in report.devices] == [False]
    assert report.devices[0].reason == "excluded by a reviewer"
    assert "GPU counted" in _failed(report)


def test_a_bmc_only_machine_is_named():
    report = explain(_sub(gpus=(_ASPEED,)))

    assert "management display adapter" in report.devices[0].reason
    assert report.gpus == []


def test_a_report_with_no_graphics_at_all_points_at_the_collector():
    report = explain(_sub(inventory={"summary": {"gpus": []}}))

    detail = next(c.detail for c in report.checks if c.label == "Device enumeration")
    assert "collector-side" in detail
    assert "Device enumeration" in _failed(report)


def test_a_stale_rollup_is_named():
    report = explain(_sub())          # nothing rolled up yet

    assert report.published_rows == 0
    assert report.verdict_ok is False
    assert "has not run" in report.verdict


def test_a_rollup_that_predates_the_fix_is_named():
    sub = _sub()
    services.rebuild_survey_stats()
    SurveyStat.objects.filter(dimension="gpu_vendor").delete()

    report = explain(sub)

    assert report.verdict_ok is False
    assert "no such row is published" in report.verdict


def test_the_active_rules_are_reported_so_nobody_has_to_recall_them():
    ComponentExclusionRule.objects.create(
        vendor_id="8086", device_id="4680", kind="gpu", reason="onboard iGPU",
    )

    assert len(explain(_sub()).rules) == 1


def test_explaining_writes_nothing():
    sub = _sub()
    before = (sub.review_state, sub.virtual, sub.gpu_vendor)

    explain(sub)

    sub.refresh_from_db()
    assert (sub.review_state, sub.virtual, sub.gpu_vendor) == before


# --- the two renderers -----------------------------------------------------------

def test_the_reviewer_page_shows_the_account(client):
    sub = _sub()
    services.rebuild_survey_stats()      # so the page reports the settled, counted case
    reviewer = User.objects.create_user("explain-rev", password="pw")
    reviewer.groups.add(Group.objects.get_or_create(name="reviewer")[0])
    client.force_login(reviewer)

    response = client.get(reverse("review:survey_submission_detail", args=[sub.pk]))
    body = response.content.decode()

    assert response.context["explanation"].gpus == ["NVIDIA"]
    assert "Why this machine is or is not in the statistics" in body
    assert "Bare metal" in body
    assert "Counted, and the published figures agree" in body


def test_the_page_names_the_failing_gate(client):
    sub = _sub(virtual=True, virt_kind="kvm")
    reviewer = User.objects.create_user("explain-rev2", password="pw")
    reviewer.groups.add(Group.objects.get_or_create(name="reviewer")[0])
    client.force_login(reviewer)

    body = client.get(
        reverse("review:survey_submission_detail", args=[sub.pk])
    ).content.decode()

    assert "virtual machine" in body
    assert "contributes nothing" in body


def test_the_command_prints_the_same_account():
    _sub()
    out = StringIO()

    call_command("survey_explain", stdout=out)
    printed = out.getvalue()

    assert "RESULT" in printed
    assert "NVIDIA" in printed
    assert "exclusion rule(s) active" in printed


def test_the_command_can_be_pointed_at_one_machine():
    _sub(identity_hash="aaaa1111")
    _sub(identity_hash="bbbb2222", cpu_vendor="AuthenticAMD")
    out = StringIO()

    call_command("survey_explain", "bbbb2222", stdout=out)

    assert "bbbb2222" in out.getvalue()
    assert "aaaa1111" not in out.getvalue()


def test_an_unknown_identity_says_so():
    _sub()
    out = StringIO()

    call_command("survey_explain", "zzzzzzzz", stdout=out)

    assert "No submissions matched" in out.getvalue()
