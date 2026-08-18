"""Certification gating: what blocks a run, and a reviewer's power to waive a specific failure.

Two rules meet here:

* A run is gated only by **genuine validations** - REQUIRED or CONDITIONAL tests. Informational
  tests and benchmarks (which carry no severity) are evidence, never gates. A benchmark that errors -
  the reported case was ``bench.gpu.clpeak`` failing on a headless server's BMC - must not stop the
  machine certifying.
* A reviewer may **waive** one failing validation, with a reason, so the run certifies in spite of
  it. The failure stays recorded and shown; it is only not counted.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import Group, User
from django.urls import reverse

from lumina.results import ingest, services
from lumina.results.tests import factories as f

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _releases():
    from lumina.releases.models import AlmaLinuxRelease
    for major in (8, 9, 10):
        AlmaLinuxRelease.objects.get_or_create(major=major, defaults={"supported": True})


@pytest.fixture
def submitter():
    return User.objects.create_user("waiver-sub")


@pytest.fixture
def reviewer():
    user = User.objects.create_user("waiver-rev")
    group, _ = Group.objects.get_or_create(name="reviewer")
    user.groups.add(group)
    return user


def _bench_error(test_id="bench.gpu.clpeak", category="gpu"):
    """A benchmark that errored: run_type benchmark, no severity, no metrics - exactly what the suite
    sends for ``bench_error`` and what blocked the reported run."""
    return {
        "id": test_id, "run_type": "benchmark", "category": category,
        "severity": None, "status": "error",
        "reason": "clpeak produced no parseable results on any of: vulkan",
        "started_at": "2026-07-27T10:05:00Z", "duration_s": 1.0, "metrics": [], "details": {},
        "artifacts": [],
    }


def _ingest(submitter, results):
    report = f.make_report(run_types=["validate"], results=results)
    return ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(f.build_bundle(report)), source="api",
    )


# --- what gates -----------------------------------------------------------------


def test_a_benchmark_error_does_not_block_certification(submitter):
    """The reported bug: a benchmark carries no severity, so it is not a gating validation and an
    error on it must not fail the run."""
    run = _ingest(submitter, [
        f.validate_result("validate.cpu.functional", status="pass"),
        _bench_error(),
    ])
    assert run.verdict() is True


def test_an_informational_failure_does_not_block(submitter):
    run = _ingest(submitter, [
        f.validate_result("validate.cpu.functional", status="pass"),
        f.validate_result("validate.gpu.driver", status="fail", severity="informational",
                          category="gpu"),
    ])
    assert run.verdict() is True


def test_a_required_failure_blocks(submitter):
    run = _ingest(submitter, [
        f.validate_result("validate.cpu.functional", status="pass"),
        f.validate_result("validate.mem.ecc", status="fail", severity="required", category="memory"),
    ])
    assert run.verdict() is False


def test_a_conditional_error_blocks(submitter):
    run = _ingest(submitter, [
        f.validate_result("validate.cpu.functional", status="pass"),
        f.validate_result("validate.net.link", status="error", severity="conditional",
                          category="network"),
    ])
    assert run.verdict() is False


# --- the waiver -----------------------------------------------------------------


def test_waiving_a_required_failure_flips_the_verdict(submitter, reviewer):
    run = _ingest(submitter, [
        f.validate_result("validate.cpu.functional", status="pass"),
        f.validate_result("validate.mem.ecc", status="fail", severity="required", category="memory"),
    ])
    assert run.verdict() is False

    result = run.results.get(test_id="validate.mem.ecc")
    services.waive_result(result, by=reviewer, reason="Known-benign on this board revision")

    result.refresh_from_db()
    assert result.is_waived and result.waived_by == reviewer
    assert result.waiver_reason == "Known-benign on this board revision"
    assert run.verdict() is True
    # certifies() follows the verdict, so approval will actually move the listing's standing.
    assert services.certifies(run) is True


def test_unwaiving_restores_the_block(submitter, reviewer):
    run = _ingest(submitter, [
        f.validate_result("validate.cpu.functional", status="pass"),
        f.validate_result("validate.mem.ecc", status="fail", severity="required", category="memory"),
    ])
    result = run.results.get(test_id="validate.mem.ecc")
    services.waive_result(result, by=reviewer, reason="benign")
    assert run.verdict() is True

    services.unwaive_result(result, by=reviewer)
    result.refresh_from_db()
    assert not result.is_waived
    assert run.verdict() is False


def test_a_waiver_needs_a_reason(submitter, reviewer):
    run = _ingest(submitter, [
        f.validate_result("validate.mem.ecc", status="fail", severity="required", category="memory"),
    ])
    result = run.results.get(test_id="validate.mem.ecc")
    with pytest.raises(services.ReviewError, match="reason"):
        services.waive_result(result, by=reviewer, reason="   ")
    result.refresh_from_db()
    assert not result.is_waived


def test_only_a_gating_failure_can_be_waived(submitter, reviewer):
    """A pass, and a non-gating result (a benchmark error), both refuse: neither blocks, so waiving
    would mean nothing."""
    run = _ingest(submitter, [
        f.validate_result("validate.cpu.functional", status="pass"),
        _bench_error(),
    ])
    passing = run.results.get(test_id="validate.cpu.functional")
    bench = run.results.get(test_id="bench.gpu.clpeak")
    with pytest.raises(services.ReviewError, match="does not block"):
        services.waive_result(passing, by=reviewer, reason="x")
    with pytest.raises(services.ReviewError, match="does not block"):
        services.waive_result(bench, by=reviewer, reason="x")


def test_unwaive_refuses_when_not_waived(submitter, reviewer):
    run = _ingest(submitter, [
        f.validate_result("validate.mem.ecc", status="fail", severity="required", category="memory"),
    ])
    result = run.results.get(test_id="validate.mem.ecc")
    with pytest.raises(services.ReviewError, match="not waived"):
        services.unwaive_result(result, by=reviewer)


# --- the blocking predicate itself ----------------------------------------------


def test_blocking_queryset_and_property_agree(submitter, reviewer):
    run = _ingest(submitter, [
        f.validate_result("validate.cpu.functional", status="pass"),
        f.validate_result("validate.mem.ecc", status="fail", severity="required", category="memory"),
        f.validate_result("validate.gpu.driver", status="fail", severity="informational",
                          category="gpu"),
        _bench_error(),
    ])
    blocking = list(run.results.blocking())
    assert [r.test_id for r in blocking] == ["validate.mem.ecc"]
    # The in-memory twin used by verdict()'s prefetch path must agree with the queryset.
    for result in run.results.all():
        assert result.is_blocking == (result in blocking)

    services.waive_result(run.results.get(test_id="validate.mem.ecc"), by=reviewer, reason="x")
    assert not run.results.blocking().exists()
    assert [r.test_id for r in run.results.waived()] == ["validate.mem.ecc"]


# --- the reviewer's controls ----------------------------------------------------


def test_reviewer_can_waive_and_unwaive_through_the_view(client, submitter, reviewer):
    run = _ingest(submitter, [
        f.validate_result("validate.cpu.functional", status="pass"),
        f.validate_result("validate.mem.ecc", status="fail", severity="required", category="memory"),
    ])
    result = run.results.get(test_id="validate.mem.ecc")
    client.force_login(reviewer)

    resp = client.post(reverse("review:run_waive_result", args=[result.pk]),
                       {"reason": "benign firmware quirk"})
    assert resp.status_code == 302
    result.refresh_from_db()
    assert result.is_waived and run.verdict() is True

    resp = client.post(reverse("review:run_unwaive_result", args=[result.pk]))
    assert resp.status_code == 302
    result.refresh_from_db()
    assert not result.is_waived and run.verdict() is False


def test_a_non_reviewer_cannot_waive(client, submitter):
    run = _ingest(submitter, [
        f.validate_result("validate.mem.ecc", status="fail", severity="required", category="memory"),
    ])
    result = run.results.get(test_id="validate.mem.ecc")
    client.force_login(submitter)   # the submitter is not a reviewer

    client.post(reverse("review:run_waive_result", args=[result.pk]), {"reason": "let me in"})

    result.refresh_from_db()
    assert not result.is_waived, "only reviewers may waive"


def test_the_review_page_offers_a_waive_control_and_notes_waivers(client, submitter, reviewer):
    run = _ingest(submitter, [
        f.validate_result("validate.cpu.functional", status="pass"),
        f.validate_result("validate.mem.ecc", status="fail", severity="required", category="memory"),
    ])
    result = run.results.get(test_id="validate.mem.ecc")
    client.force_login(reviewer)

    html = client.get(reverse("review:run_detail", args=[run.pk])).content.decode()
    assert reverse("review:run_waive_result", args=[result.pk]) in html

    services.waive_result(result, by=reviewer, reason="benign firmware quirk")
    html = client.get(reverse("review:run_detail", args=[run.pk])).content.decode()
    assert "Waived failures" in html
    assert "benign firmware quirk" in html
    # The verdict now reads PASS with the note, per the "plain PASS + note" choice.
    assert "waived by a reviewer" in html
