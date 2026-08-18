"""Why a listing on my dashboard is not published, and what would change that.

Reported from a real dashboard: unpublished components with "no indication of why
they're there or instructions on how to get them into any other status." Measuring the
devstack database found three of them under one user, all linked to a **rejected** run
whose ``host_os_id`` was ``fedora`` - so the guess that a non-AlmaLinux run was behind it
was right - and one of the three was a seeded GPU *family* the user had never created.

Four different situations were rendering as the same word:

1. Waiting on a reviewer. Nothing to do, and saying so is the answer.
2. Blocked. The run that would have published it was rejected or quarantined, and a
   rejected run is terminal (``SUBMITTABLE_STATUSES`` is draft and needs-changes), so
   nothing about that run will ever publish the listing. Only a fresh passing run will.
3. A shared catalog entry. ``hardware/0003_reference_data.py`` seeds 81 CPU and GPU
   families unpublished on purpose, and the dashboard matches anything a user's runs are
   linked to - a run is linked to the family it was classified against. The reader cannot
   publish one and should not be asked to.
4. Genuinely nothing has validated it yet.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.files.base import ContentFile
from django.urls import reverse
from django.utils import timezone

from lumina.core.certification import ValidationLevel
from lumina.hardware.models import Component, ComponentKind, Submission, System
from lumina.hardware.services import (
    PUBLICATION_BLOCKED,
    PUBLICATION_REFERENCE,
    PUBLICATION_UNPROVEN,
    PUBLICATION_WAITING,
    publication_state,
)
from lumina.releases.models import AlmaLinuxRelease
from lumina.results.models import RunType, TestRun
from lumina.vendors.models import Vendor

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture
def dell():
    return Vendor.objects.create(name="Dell Inc.", published=True)


@pytest.fixture
def owner():
    return User.objects.create_user("owner", password="pw")


def _run(submitter, *, status, host_os_id="almalinux", listing=None, component=None):
    release, _ = AlmaLinuxRelease.objects.get_or_create(
        major=9, defaults={"supported": True},
    )
    run = TestRun.objects.create(
        run_type=RunType.validate.value,
        schema_version="1.0", suite_version="0.1.0",
        submitter=submitter, source="api",
        bundle=ContentFile(b"x", name=f"b{TestRun.objects.count()}.tar.zst"),
        bundle_sha256=f"{TestRun.objects.count():064d}",
        status=status, alma_release=release, host_os_id=host_os_id,
        published_at=timezone.now() if status == TestRun.STATUS_APPROVED else None,
        listing_system=listing,
    )
    if component is not None:
        run.listing_components.add(component)
    return run


def test_a_published_listing_has_nothing_to_explain(dell, owner):
    listing = System.objects.create(
        vendor=dell, name="PowerEdge R750", published=True, created_by=owner,
    )

    assert publication_state(listing, owner) is None


def test_a_rejected_run_says_it_is_a_dead_end(dell, owner):
    """The reported case. A rejected run cannot be resubmitted, so the listing will
    never publish from it - and nothing said so."""
    listing = System.objects.create(
        vendor=dell, name="PowerEdge R750", created_by=owner,
    )
    _run(owner, status=TestRun.STATUS_REJECTED, host_os_id="fedora", listing=listing)

    state = publication_state(listing, owner)

    assert state["kind"] == PUBLICATION_BLOCKED
    assert "rejected" in state["reason"]
    assert "cannot be resubmitted" in state["reason"]
    # The actual cause, named.
    assert "fedora" in state["reason"]
    assert "AlmaLinux" in state["next_step"]


def test_a_rejected_almalinux_run_does_not_blame_the_operating_system(dell, owner):
    """A run can be rejected on its merits. Claiming the OS was wrong when it was not
    sends the reader off to fix something that is already correct."""
    listing = System.objects.create(
        vendor=dell, name="PowerEdge R750", created_by=owner,
    )
    _run(owner, status=TestRun.STATUS_REJECTED, host_os_id="almalinux", listing=listing)

    state = publication_state(listing, owner)

    assert state["kind"] == PUBLICATION_BLOCKED
    assert "not AlmaLinux" not in state["reason"]


def test_a_quarantined_run_says_which_os_it_was(dell, owner):
    """Going forward this is the state a Fedora run lands in, rather than rejected."""
    listing = System.objects.create(
        vendor=dell, name="PowerEdge R750", created_by=owner,
    )
    _run(owner, status=TestRun.STATUS_QUARANTINED, host_os_id="rocky", listing=listing)

    state = publication_state(listing, owner)

    assert state["kind"] == PUBLICATION_BLOCKED
    assert "rocky" in state["reason"]
    assert "not AlmaLinux" in state["reason"]


def test_a_pending_run_says_to_wait(dell, owner):
    listing = System.objects.create(
        vendor=dell, name="PowerEdge R750", created_by=owner,
    )
    _run(owner, status=TestRun.STATUS_PENDING, listing=listing)

    state = publication_state(listing, owner)

    assert state["kind"] == PUBLICATION_WAITING
    assert "Nothing to do" in state["next_step"]


def test_a_draft_run_says_to_submit_it(dell, owner):
    listing = System.objects.create(
        vendor=dell, name="PowerEdge R750", created_by=owner,
    )
    _run(owner, status=TestRun.STATUS_DRAFT, listing=listing)

    state = publication_state(listing, owner)

    assert state["kind"] == PUBLICATION_WAITING
    assert "not been submitted" in state["reason"]


def test_a_pending_submission_says_to_wait(dell, owner):
    listing = System.objects.create(
        vendor=dell, name="PowerEdge R750", created_by=owner,
    )
    Submission.objects.create(
        submitter=owner, listing_system=listing,
        claimed_validation_level=ValidationLevel.COMMUNITY,
    )

    state = publication_state(listing, owner)

    assert state["kind"] == PUBLICATION_WAITING
    assert "Waiting for a reviewer" in state["reason"]


def test_a_bounced_submission_points_at_revising(dell, owner):
    listing = System.objects.create(
        vendor=dell, name="PowerEdge R750", created_by=owner,
    )
    submission = Submission.objects.create(
        submitter=owner, listing_system=listing,
        claimed_validation_level=ValidationLevel.COMMUNITY,
    )
    reviewer = User.objects.create_user("rev")
    group, _ = Group.objects.get_or_create(name="reviewer")
    reviewer.groups.add(group)
    submission.request_changes(by=reviewer, reason="more detail please")

    state = publication_state(listing, owner)

    assert state["kind"] == PUBLICATION_WAITING
    assert "asked for changes" in state["reason"]
    assert "Revise" in state["next_step"]


def test_an_open_submission_outranks_an_old_rejected_run(dell, owner):
    """Order matters. A submitter who filed a submission after a run was rejected should
    be told to wait, not told to go and do the thing they have already done."""
    listing = System.objects.create(
        vendor=dell, name="PowerEdge R750", created_by=owner,
    )
    _run(owner, status=TestRun.STATUS_REJECTED, host_os_id="fedora", listing=listing)
    Submission.objects.create(
        submitter=owner, listing_system=listing,
        claimed_validation_level=ValidationLevel.COMMUNITY,
    )

    assert publication_state(listing, owner)["kind"] == PUBLICATION_WAITING


# --- the shared-catalog case ----------------------------------------------------


def test_a_seeded_family_is_named_as_a_shared_catalog_entry(owner):
    """The one the reader cannot act on, and the one they never created.

    ``created_by`` is NULL on every family the reference-data migration seeds.
    """
    # get_or_create: the reference-data migration already seeds AMD, and a
    # second row collides on the unique slug.
    amd, _ = Vendor.objects.get_or_create(
        name="AMD", defaults={"published": True},
    )
    family = Component.objects.create(
        vendor=amd, name="Intel Arc A-Series (Alchemist)",
        kind=ComponentKind.gpu.value, published=False,
    )

    state = publication_state(family, owner)

    assert state["kind"] == PUBLICATION_REFERENCE
    assert state["shared"] is True
    assert "Nothing to do" in state["next_step"]


def test_a_shared_family_with_a_rejected_run_still_says_it_is_shared(owner):
    """``shared`` is reported alongside the state, not instead of it.

    This is the exact row that prompted the report: a seeded GPU family, linked to the
    reader's own rejected Fedora run, appearing under "my components". Both facts matter
    - what would publish it, and why it is on their page at all - and an earlier version
    let the blocked branch shadow the shared one entirely.
    """
    # get_or_create: the reference-data migration already seeds AMD, and a
    # second row collides on the unique slug.
    amd, _ = Vendor.objects.get_or_create(
        name="AMD", defaults={"published": True},
    )
    family = Component.objects.create(
        vendor=amd, name="Raphael", kind=ComponentKind.gpu.value, published=False,
    )
    _run(owner, status=TestRun.STATUS_REJECTED, host_os_id="fedora", component=family)

    state = publication_state(family, owner)

    assert state["kind"] == PUBLICATION_BLOCKED
    assert state["shared"] is True


def test_somebody_elses_listing_counts_as_shared(dell, owner):
    other = User.objects.create_user("someone-else")
    listing = System.objects.create(
        vendor=dell, name="PowerEdge R750", created_by=other,
    )

    assert publication_state(listing, owner)["shared"] is True


def test_my_own_untouched_listing_is_not_shared(dell, owner):
    listing = System.objects.create(
        vendor=dell, name="PowerEdge R750", created_by=owner,
    )

    state = publication_state(listing, owner)

    assert state["kind"] == PUBLICATION_UNPROVEN
    assert state["shared"] is False


def test_every_state_carries_the_shared_flag(dell, owner):
    """It is read unconditionally by the template, and a missing key is silently falsy
    in a Django template - so a branch that forgot it would look like "not shared"."""
    listing = System.objects.create(
        vendor=dell, name="PowerEdge R750", created_by=owner,
    )
    cases = [
        None,
        TestRun.STATUS_DRAFT,
        TestRun.STATUS_PENDING,
        TestRun.STATUS_REJECTED,
        TestRun.STATUS_QUARANTINED,
    ]
    for status in cases:
        TestRun.objects.all().delete()
        if status is not None:
            _run(owner, status=status, listing=listing)
        state = publication_state(listing, owner)
        assert "shared" in state, status
        assert "reason" in state and "next_step" in state, status


# --- on the page ----------------------------------------------------------------


def test_the_dashboard_explains_a_blocked_component(client, dell, owner):
    """End to end, because the explanation is worthless if the page does not show it."""
    component = Component.objects.create(
        vendor=dell, name="B650M PG Riptide", kind=ComponentKind.motherboard.value,
        published=False, created_by=owner,
    )
    _run(owner, status=TestRun.STATUS_REJECTED, host_os_id="fedora",
         component=component)
    client.force_login(owner)

    body = client.get(reverse("accounts:dashboard")).content.decode()

    assert "B650M PG Riptide" in body
    assert "cannot be resubmitted" in body
    assert "fedora" in body


def test_the_dashboard_says_when_a_row_is_not_the_readers_listing(client, owner):
    # get_or_create: the reference-data migration already seeds AMD, and a
    # second row collides on the unique slug.
    amd, _ = Vendor.objects.get_or_create(
        name="AMD", defaults={"published": True},
    )
    family = Component.objects.create(
        vendor=amd, name="Raphael", kind=ComponentKind.gpu.value, published=False,
    )
    _run(owner, status=TestRun.STATUS_REJECTED, host_os_id="fedora", component=family)
    client.force_login(owner)

    body = client.get(reverse("accounts:dashboard")).content.decode()

    assert "Shared catalog entry" in body


def test_a_published_row_gets_no_explanation(client, dell, owner):
    """The copy is for a reader who is stuck. On a published listing it would be noise."""
    System.objects.create(
        vendor=dell, name="PowerEdge R750", published=True, created_by=owner,
    )
    client.force_login(owner)

    body = client.get(reverse("accounts:dashboard")).content.decode()

    assert "cannot be resubmitted" not in body
    assert "Shared catalog entry" not in body
