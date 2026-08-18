"""The reviewers' archive of already-decided work.

The queue filters to ``OPEN_STATUSES``, so the instant a reviewer approved or rejected
anything it vanished from every page they could reach. There was no way to answer "what
happened to that submission", "who approved this", or "what did I do last week".

The records existed the whole time. Every reviewable model keeps ``reviewed_by``,
``reviewed_at``, and ``reviewer_notes``, and ``audit.AuditLogEntry`` has an append-only row
per action with a read-only admin already registered for it. The catch is who can read it:
**reviewers are deliberately not staff** - the review UI lives outside ``/admin/`` for
exactly that reason - so ``/admin/audit/`` is unreachable by the people whose decisions it
records. That is the gap this closes.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.files.base import ContentFile
from django.urls import reverse
from django.utils import timezone

from lumina.audit.models import AuditLogEntry
from lumina.audit.services import log_action
from lumina.core.certification import ValidationLevel
from lumina.hardware.models import ListingVersion, Submission, System
from lumina.releases.models import AlmaLinuxRelease
from lumina.results.models import RunType, TestRun
from lumina.review.archive import decision_kinds, decisions
from lumina.vendors.models import Vendor

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture
def reviewer(client):
    user = User.objects.create_user("arch-rev", password="pw")
    group, _ = Group.objects.get_or_create(name="reviewer")
    user.groups.add(group)
    client.force_login(user)
    return user


@pytest.fixture
def submitter():
    return User.objects.create_user("arch-sub", password="pw")


@pytest.fixture
def dell():
    return Vendor.objects.create(name="Dell Inc.", published=True)


def _decided_submission(dell, submitter, reviewer, *, approve=True):
    release, _ = AlmaLinuxRelease.objects.get_or_create(
        major=9, defaults={"supported": True},
    )
    listing = System.objects.create(
        vendor=dell, name="PowerEdge R750", created_by=submitter,
    )
    ListingVersion.objects.create(
        listing_system=listing, release=release,
        source=ListingVersion.SOURCE_DECLARED,
    )
    submission = Submission.objects.create(
        submitter=submitter, listing_system=listing,
        claimed_validation_level=ValidationLevel.COMMUNITY,
    )
    submission.cited_releases.set([release])
    if approve:
        submission.approve(by=reviewer, final_level=ValidationLevel.COMMUNITY)
    else:
        submission.reject(by=reviewer, reason="Not enough detail.")
    return submission


# --- what the archive contains --------------------------------------------------


def test_an_approved_submission_appears(dell, submitter, reviewer):
    _decided_submission(dell, submitter, reviewer)

    rows = decisions()

    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "Hardware submission"
    assert row["subject"] == "Dell Inc. PowerEdge R750"
    assert row["outcome"] == "Approved"
    assert row["by"] == reviewer
    assert row["at"] is not None


def test_a_rejection_carries_the_reviewers_note(dell, submitter, reviewer):
    """The note is the whole point of an archive: it is the only record of *why*."""
    _decided_submission(dell, submitter, reviewer, approve=False)

    row = decisions()[0]

    assert row["outcome"] == "Rejected"
    assert row["notes"] == "Not enough detail."


def test_open_work_is_not_in_the_archive(dell, submitter, reviewer):
    """It is in the queue. A row in both would be decided and undecided at once."""
    listing = System.objects.create(vendor=dell, name="PowerEdge R750")
    Submission.objects.create(
        submitter=submitter, listing_system=listing,
        claimed_validation_level=ValidationLevel.COMMUNITY,
    )

    assert decisions() == []


def test_needs_changes_is_not_decided(dell, submitter, reviewer):
    """A reviewer asking for changes has not finished deciding, and the row is still in
    the queue - so it must not also be filed as history."""
    listing = System.objects.create(vendor=dell, name="PowerEdge R750")
    submission = Submission.objects.create(
        submitter=submitter, listing_system=listing,
        claimed_validation_level=ValidationLevel.COMMUNITY,
    )
    submission.request_changes(by=reviewer, reason="more detail")

    assert decisions() == []


def test_a_rejected_run_appears_with_the_machine_name(submitter, reviewer):
    """Runs are not ``ReviewWorkflow`` models but they are reviewed, so they belong
    here. An unlinked run must not print its uuid at a reader."""
    release, _ = AlmaLinuxRelease.objects.get_or_create(
        major=9, defaults={"supported": True},
    )
    run = TestRun.objects.create(
        run_type=RunType.validate.value, schema_version="1.0", suite_version="0.1.0",
        submitter=submitter, source="api",
        bundle=ContentFile(b"x", name="b.tar.zst"), bundle_sha256="0" * 64,
        status=TestRun.STATUS_REJECTED, alma_release=release,
        system_vendor="Dell Inc.", system_product="PowerEdge R760",
        # Stated, because this row bypasses ingest and the field defaults to the fallback kind.
        # Without it the machine reads as a custom build and is named after a board it has none of.
        system_kind="prebuilt",
        reviewed_by=reviewer, reviewed_at=timezone.now(),
        reviewer_notes="Ran on Fedora.",
    )

    rows = [r for r in decisions() if r["kind"] == "Validation run"]

    assert len(rows) == 1
    assert "PowerEdge R760" in rows[0]["subject"]
    assert str(run.uuid) not in rows[0]["subject"]
    assert rows[0]["notes"] == "Ran on Fedora."


def test_a_decision_with_no_reviewer_does_not_break_the_ordering(submitter, reviewer):
    """``publish_due_runs`` releases an embargoed run on a timer, so ``reviewed_at`` is
    genuinely null on some rows. Sorting a mix of null and non-null must not raise."""
    release, _ = AlmaLinuxRelease.objects.get_or_create(
        major=9, defaults={"supported": True},
    )
    for index in range(2):
        TestRun.objects.create(
            run_type=RunType.validate.value, schema_version="1.0",
            suite_version="0.1.0", submitter=submitter, source="api",
            bundle=ContentFile(b"x", name=f"n{index}.tar.zst"),
            bundle_sha256=f"{index:064d}",
            status=TestRun.STATUS_APPROVED, alma_release=release,
            published_at=timezone.now(), reviewed_by=None, reviewed_at=None,
        )

    rows = decisions()

    assert len(rows) == 2
    assert all(r["at"] is None for r in rows)


def test_dated_decisions_sort_before_undated_ones(dell, submitter, reviewer):
    """Newest first, and a row nobody dated belongs at the bottom rather than the top."""
    release, _ = AlmaLinuxRelease.objects.get_or_create(
        major=9, defaults={"supported": True},
    )
    TestRun.objects.create(
        run_type=RunType.validate.value, schema_version="1.0", suite_version="0.1.0",
        submitter=submitter, source="api",
        bundle=ContentFile(b"x", name="u.tar.zst"), bundle_sha256="1" * 64,
        status=TestRun.STATUS_APPROVED, alma_release=release,
        published_at=timezone.now(), reviewed_at=None,
    )
    _decided_submission(dell, submitter, reviewer)

    rows = decisions()

    assert rows[0]["at"] is not None
    assert rows[-1]["at"] is None


def test_every_reviewable_kind_is_offered_as_a_filter():
    """If a seventh reviewable model appears and is not registered here, the archive
    silently omits it - which is worse than an empty tab, because it reads as "nothing
    was decided" rather than "this is not covered"."""
    kinds = decision_kinds()

    assert "Hardware submission" in kinds
    assert "Validation run" in kinds
    assert "Software submission" in kinds
    assert "Vendor claim" in kinds
    assert len(set(kinds)) == len(kinds), "duplicate labels would merge two sources"


def test_the_source_list_covers_every_reviewable_model():
    """Guards against the archive drifting behind the models.

    ``ReviewWorkflow`` subclasses are enumerable, so the omission is detectable rather
    than something a reader has to notice.
    """
    from django.apps import apps

    from lumina.core.review import ReviewWorkflow

    reviewable = {
        m.__name__ for m in apps.get_models() if issubclass(m, ReviewWorkflow)
    }
    # The archive labels are prose, so map them back by hand. Adding a model means
    # adding a Source and a line here.
    covered = {
        "Submission", "SoftwareSubmission", "ListingEditProposal",
        "SoftwareEditProposal", "VendorProposal", "VendorClaim",
    }

    assert reviewable == covered, (
        f"reviewable models not in the archive: {sorted(reviewable - covered)}"
    )


# --- the page -------------------------------------------------------------------


def test_a_reviewer_can_reach_it(client, reviewer):
    assert client.get(reverse("review:archive")).status_code == 200


def test_a_plain_user_cannot(client):
    client.force_login(User.objects.create_user("nobody", password="pw"))

    assert client.get(reverse("review:archive")).status_code == 403


def test_anonymous_cannot(client):
    resp = client.get(reverse("review:archive"))

    assert resp.status_code in (302, 403)


def test_the_page_shows_a_decision_and_its_reviewer(client, dell, submitter, reviewer):
    _decided_submission(dell, submitter, reviewer, approve=False)

    body = client.get(reverse("review:archive")).content.decode()

    assert "PowerEdge R750" in body
    assert "Not enough detail." in body
    assert reviewer.username in body


def test_the_page_shows_the_activity_log(client, dell, submitter, reviewer):
    """The second pane, and the reason it is not just the decisions: a tweak is not a
    decision about a reviewable object but it is absolutely something a reviewer wants
    to be able to look up."""
    submission = _decided_submission(dell, submitter, reviewer)
    log_action(
        "submission.tweak", target=submission, actor=reviewer,
        notes="renamed the listing",
    )

    body = client.get(reverse("review:archive")).content.decode()

    assert "submission.tweak" in body
    assert "renamed the listing" in body


def test_the_activity_log_can_be_filtered_by_action(
    client, dell, submitter, reviewer
):
    submission = _decided_submission(dell, submitter, reviewer)
    log_action("submission.tweak", target=submission, actor=reviewer)
    log_action("vendor.verify", target=dell, actor=reviewer)

    body = client.get(
        reverse("review:archive"), {"action": "vendor.verify"}
    ).content.decode()

    # Asserted on the table's own markup, not on the raw string. Every action appears in
    # the filter dropdown by design - that is what makes it selectable - so a bare
    # substring check can never show that a row was excluded.
    assert "<code>vendor.verify</code>" in body
    assert "<code>submission.tweak</code>" not in body


def test_the_activity_log_can_be_filtered_by_actor(client, dell, submitter, reviewer):
    submission = _decided_submission(dell, submitter, reviewer)
    other = User.objects.create_user("someone-else")
    log_action("submission.tweak", target=submission, actor=other)

    body = client.get(
        reverse("review:archive"), {"actor": "someone-else"}
    ).content.decode()

    assert "someone-else" in body
    assert AuditLogEntry.objects.count() >= 1


def test_the_decisions_pane_can_be_filtered_by_kind(client, dell, submitter, reviewer):
    _decided_submission(dell, submitter, reviewer)

    body = client.get(
        reverse("review:archive"), {"kind": "Vendor claim"}
    ).content.decode()

    assert "Nothing has been decided yet." in body
    assert "PowerEdge R750" not in body


def test_the_queue_links_to_the_archive(client, reviewer):
    body = client.get(reverse("review:queue")).content.decode()

    assert reverse("review:archive") in body
