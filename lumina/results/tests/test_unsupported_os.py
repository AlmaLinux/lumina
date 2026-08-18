"""Runs that were not performed on AlmaLinux are quarantined, not certified.

``environment.os.id`` has been in every bundle since schema v1.0 and nothing read
it. ``parse_release`` looked only at ``version_id``, and **every RHEL rebuild
numbers its releases the same way**, so a Rocky 9.6 or RHEL 9.6 report parsed to
(9, 6) and matched ``AlmaLinuxRelease(major=9)`` exactly as an AlmaLinux run did.
Approving one would have recorded AlmaLinux 9 compatibility on the hardware and
credited a community attestation for it.

alma-cert refuses to upload such a run, so anything arriving here is an old client,
a modified one, or a hand-built bundle. It is **kept rather than refused** so an
attempted submission is visible to a reviewer instead of merely bounced, and held
outside every path that could turn it into a certification:

- not an ``OPEN_STATUS``, so ``_require_open`` refuses to approve it,
- ``public()`` filters on approved, so it can never reach a public page,
- no ``alma_release`` is bound, so ``record_compatibility`` and ``_attest_one``
  have nothing to attach to even if something did approve it, and
- ``_require_supported_os`` checks the reported OS independently of status, so
  editing the status by hand does not get around it.

A reviewer who can see the report is wrong about the OS releases it explicitly,
with a reason, and it rejoins the normal queue.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from lumina.hardware.models import CommunityAttestation, ListingVersion, System
from lumina.releases.models import AlmaLinuxRelease
from lumina.results import ingest, services
from lumina.results.models import TestRun
from lumina.results.tests import factories as f
from lumina.vendors.models import Vendor

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture(autouse=True)
def releases():
    for major in (8, 9, 10):
        AlmaLinuxRelease.objects.get_or_create(
            major=major, defaults={"supported": True},
        )


@pytest.fixture
def submitter():
    return User.objects.create_user("submitter", email="s@example.com")


@pytest.fixture
def reviewer(django_user_model):
    """``reviewer_required`` checks group membership, not is_staff."""
    from django.contrib.auth.models import Group

    user = django_user_model.objects.create_user("reviewer", password="pw")
    user.groups.add(Group.objects.create(name="reviewer"))
    return user


def _ingest(report, **kwargs):
    bundle = f.as_upload(f.build_bundle(report))
    kwargs.setdefault("source", "api")
    return ingest.ingest_bundle(bundle_file=bundle, **kwargs)


def _run(submitter, *, os_id, run_types=None, results=None, version_id="9.6"):
    return _ingest(
        f.make_report(
            os_id=os_id,
            version_id=version_id,
            run_types=run_types or ["collect", "validate"],
            results=results or [f.validate_result("validate.cpu.functional")],
        ),
        submitter=submitter,
    )


# --- ingest -------------------------------------------------------------------


def test_an_almalinux_run_is_unaffected(submitter):
    run = _run(submitter, os_id="almalinux")

    assert run.status == TestRun.STATUS_DRAFT
    assert run.host_os_id == "almalinux"
    assert run.alma_release.major == 9
    assert run.alma_minor == 6


@pytest.mark.parametrize("os_id", ["rocky", "rhel", "centos", "fedora", "ubuntu"])
def test_another_distribution_is_quarantined(submitter, os_id):
    run = _run(submitter, os_id=os_id)

    assert run.status == TestRun.STATUS_QUARANTINED
    assert run.host_os_id == os_id


def test_a_rebuild_is_not_bound_to_an_almalinux_release(submitter):
    """The defect, stated directly. Rocky 9.6 parses to (9, 6) like AlmaLinux 9.6,
    so without the OS check it would have matched AlmaLinuxRelease(major=9)."""
    run = _run(submitter, os_id="rocky", version_id="9.6")

    assert run.alma_release is None
    assert run.alma_minor is None


def test_a_missing_os_id_is_quarantined(submitter):
    """"Cannot tell" must not mean "supported", or a stripped image is the way
    around the gate."""
    run = _run(submitter, os_id="")

    assert run.status == TestRun.STATUS_QUARANTINED


def test_the_os_id_is_matched_case_insensitively(submitter):
    """``/etc/os-release`` is only conventionally lowercase."""
    run = _run(submitter, os_id="AlmaLinux")

    assert run.status == TestRun.STATUS_DRAFT
    assert run.host_os_id == "almalinux"


def test_a_benchmark_run_is_quarantined_too(submitter):
    """The leaderboards compare AlmaLinux machines. A Rocky result ranked beside
    them is not a like-for-like comparison and would be invisible as anything
    else."""
    run = _run(submitter, os_id="rocky", run_types=["collect", "benchmark"],
               results=[f.benchmark_result()])

    assert run.status == TestRun.STATUS_QUARANTINED


# --- it cannot become certification ------------------------------------------


def test_a_quarantined_run_cannot_be_approved(submitter, reviewer):
    run = _run(submitter, os_id="rocky")

    with pytest.raises(services.ReviewError) as exc:
        services.approve_run(run, by=reviewer)

    assert "not performed on AlmaLinux" in str(exc.value)
    assert "rocky" in str(exc.value)


def test_a_quarantined_run_is_never_public(submitter):
    run = _run(submitter, os_id="rocky")

    assert run not in TestRun.objects.public()
    assert not run.is_public


def test_the_os_gate_survives_a_hand_edited_status(submitter, reviewer):
    """The layer that matters if anything else is bypassed.

    Someone flipping ``status`` in the admin would clear ``_require_open``.
    ``_require_supported_os`` reads the reported OS instead, so it still refuses.
    """
    run = _run(submitter, os_id="rocky")
    TestRun.objects.filter(pk=run.pk).update(status=TestRun.STATUS_PENDING)
    run.refresh_from_db()

    with pytest.raises(services.ReviewError) as exc:
        services.approve_run(run, by=reviewer)

    assert "rather than" in str(exc.value)
    assert run.status != TestRun.STATUS_APPROVED


def test_it_does_not_appear_in_the_reviewers_normal_queue(submitter):
    quarantined = _run(submitter, os_id="rocky")

    assert quarantined not in TestRun.objects.open_for_review()
    assert quarantined in TestRun.objects.quarantined()


def test_a_quarantined_run_can_be_rejected(submitter, reviewer):
    """Disposing of one is the ordinary outcome. Without this a reviewer could
    only release it or leave it sitting in the queue forever."""
    run = _run(submitter, os_id="rocky")

    services.reject_run(run, by=reviewer, reason="wrong distro")

    run.refresh_from_db()
    assert run.status == TestRun.STATUS_REJECTED


# --- the reviewer override ----------------------------------------------------


def test_releasing_returns_it_to_the_normal_queue(submitter, reviewer):
    run = _run(submitter, os_id="rocky")

    services.release_from_quarantine(
        run, by=reviewer, reason="minimal AlmaLinux 9 image, os-release stripped",
    )

    run.refresh_from_db()
    assert run.status == TestRun.STATUS_DRAFT       # a validate run's normal start
    assert run.os_quarantine_released is True
    assert run.may_certify_almalinux is True


def test_releasing_binds_the_almalinux_release(submitter, reviewer):
    """Ingest refused to resolve it. The reviewer has just said the OS was
    misreported, so the version numbers can be trusted after all."""
    run = _run(submitter, os_id="rocky", version_id="9.6")
    assert run.alma_release is None

    services.release_from_quarantine(run, by=reviewer, reason="it really is Alma")

    run.refresh_from_db()
    assert run.alma_release.major == 9
    assert run.alma_minor == 6


def test_releasing_requires_a_reason(submitter, reviewer):
    """The only route by which a non-AlmaLinux report becomes AlmaLinux evidence,
    so the record has to say on what grounds."""
    run = _run(submitter, os_id="rocky")

    for blank in ("", "   "):
        with pytest.raises(services.ReviewError):
            services.release_from_quarantine(run, by=reviewer, reason=blank)

    run.refresh_from_db()
    assert run.status == TestRun.STATUS_QUARANTINED


def test_only_a_quarantined_run_can_be_released(submitter, reviewer):
    run = _run(submitter, os_id="almalinux")

    with pytest.raises(services.ReviewError):
        services.release_from_quarantine(run, by=reviewer, reason="why not")


def test_the_release_is_audited(submitter, reviewer):
    from lumina.audit.models import AuditLogEntry

    run = _run(submitter, os_id="rocky")

    services.release_from_quarantine(run, by=reviewer, reason="stripped os-release")

    entry = AuditLogEntry.objects.filter(action="test_run.quarantine_release").first()
    assert entry is not None
    assert entry.actor == reviewer
    assert entry.before["host_os_id"] == "rocky"
    assert entry.notes == "stripped os-release"


def test_a_released_run_can_then_be_approved_and_certifies(submitter, reviewer):
    """End to end: the override actually leads somewhere.

    A released run has to be able to complete the normal path, or the override
    would be a button that changes a status and nothing else.
    """
    vendor = Vendor.objects.create(name="Dell Inc.", published=True)
    system = System.objects.create(vendor=vendor, name="PowerEdge R750")
    run = _run(submitter, os_id="rocky")
    services.release_from_quarantine(run, by=reviewer, reason="misreported")
    run.refresh_from_db()
    # A released validate run lands in draft, the same as any other, so it goes
    # through the ordinary route from there: the reviewer names the hardware, the
    # submitter releases it, and only then is it approvable.
    services.assign_listing(run, system=system, by=reviewer)
    run.refresh_from_db()
    services.submit_for_review(run, by=submitter)
    run.refresh_from_db()

    services.approve_run(run, by=reviewer)

    run.refresh_from_db()
    assert run.status == TestRun.STATUS_APPROVED
    assert ListingVersion.objects.filter(
        listing_system=system, release__major=9
    ).exists()
    assert CommunityAttestation.objects.filter(listing_system=system).exists()


# --- the reviewer surfaces ----------------------------------------------------


def test_the_queue_shows_a_quarantine_pane(client, submitter, reviewer):
    _run(submitter, os_id="rocky")
    client.force_login(reviewer)

    body = client.get(reverse("review:queue")).content.decode()

    assert "Not on AlmaLinux" in body
    assert "rocky" in body


def test_the_pane_is_absent_when_nothing_is_quarantined(client, submitter, reviewer):
    """An always-present tab reading zero would imply this is routine. It is an
    exception."""
    _run(submitter, os_id="almalinux")
    client.force_login(reviewer)

    body = client.get(reverse("review:queue")).content.decode()

    assert "tab-quarantined-runs" not in body


def test_the_run_page_explains_and_offers_the_release(client, submitter, reviewer):
    run = _run(submitter, os_id="rocky")
    client.force_login(reviewer)

    body = client.get(reverse("review:run_detail", args=[run.pk])).content.decode()

    assert "Not run on AlmaLinux" in body
    assert reverse("review:run_release_quarantine", args=[run.pk]) in body
    assert "only</strong> if the report is wrong" in body


def test_the_release_view_works(client, submitter, reviewer):
    run = _run(submitter, os_id="rocky")
    client.force_login(reviewer)

    client.post(
        reverse("review:run_release_quarantine", args=[run.pk]),
        {"reason": "os-release stripped by the image build"},
    )

    run.refresh_from_db()
    assert run.status == TestRun.STATUS_DRAFT


def test_the_release_view_refuses_without_a_reason(client, submitter, reviewer):
    run = _run(submitter, os_id="rocky")
    client.force_login(reviewer)

    client.post(reverse("review:run_release_quarantine", args=[run.pk]), {})

    run.refresh_from_db()
    assert run.status == TestRun.STATUS_QUARANTINED
