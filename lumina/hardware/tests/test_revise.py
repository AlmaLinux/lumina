"""Fixing a hardware submission a reviewer sent back.

Hardware had no way to do this. A reviewer could set a submission to needs-changes and
that decision reached nobody: ``accounts.views.dashboard`` never queried
``hardware.Submission``, no mail went out, ``reviewer_notes`` appeared in no
submitter-facing template, and the ``submit`` namespace held exactly one URL. The only
way to act on it was to submit again from scratch, which opened a *second* pending row
while the bounced one sat in the queue for a reviewer to clear by hand.

That mattered more after the re-validation flow was deleted, because re-POSTing against
the same listing had been the accidental workaround and it no longer exists.

Software has had ``software:revise`` all along, so this is the same view, the same
template arrangement, and now literally the same ``resubmit`` method: it moved from
``SoftwareSubmission`` onto ``ReviewWorkflow`` rather than being copied.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core import mail
from django.urls import reverse

from lumina.core.certification import ValidationLevel
from lumina.hardware.models import ListingVersion, Submission, System
from lumina.releases.models import AlmaLinuxRelease
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
def dell():
    return Vendor.objects.create(name="Dell Inc.", published=True)


@pytest.fixture
def submitter():
    return User.objects.create_user("sub", password="pw")


@pytest.fixture
def reviewer():
    user = User.objects.create_user("rev", password="pw")
    group, _ = Group.objects.get_or_create(name="reviewer")
    user.groups.add(group)
    return user


@pytest.fixture
def bounced(client, dell, submitter, reviewer):
    """A submission a reviewer has sent back, created through the real form."""
    client.force_login(submitter)
    client.post(reverse("submit:start"), {
        "kind": "system", "name": "PowerEdge R750", "model_number": "R750",
        "vendor": dell.slug,
        "claimed_validation_level": ValidationLevel.COMMUNITY,
        "release_support_9": "1", "release_min_minor_9": "0",
    })
    submission = Submission.objects.get()
    submission.request_changes(by=reviewer, reason="Please add the BIOS version.")
    mail.outbox.clear()
    return submission


def _payload(dell, **overrides):
    data = {
        "kind": "system", "name": "PowerEdge R750", "model_number": "R750",
        "vendor": dell.slug,
        "claimed_validation_level": ValidationLevel.COMMUNITY,
        "release_support_9": "1", "release_min_minor_9": "0",
    }
    data.update(overrides)
    return data


# --- the submitter can see it ---------------------------------------------------


def test_the_dashboard_shows_the_status_and_the_reviewers_note(
    client, bounced, submitter
):
    """Both were in the database and on no page the submitter could reach."""
    client.force_login(submitter)

    body = client.get(reverse("accounts:dashboard")).content.decode()

    assert "Needs changes" in body
    assert "Please add the BIOS version." in body


def test_the_dashboard_offers_a_revise_link(client, bounced, submitter):
    client.force_login(submitter)

    body = client.get(reverse("accounts:dashboard")).content.decode()

    assert reverse("submit:revise", args=[bounced.uuid]) in body


def test_a_bounced_submission_no_longer_reads_as_merely_unpublished(
    client, bounced, submitter
):
    """The old cell derived its text from the listing, so a submission sent back for
    changes and one still waiting in the queue looked identical."""
    client.force_login(submitter)

    body = client.get(reverse("accounts:dashboard")).content.decode()

    assert "Needs changes" in body
    assert body.count("unpublished") == 0


# --- revising it ----------------------------------------------------------------


def test_the_form_is_prefilled_with_what_was_submitted(client, bounced, submitter):
    """Otherwise the submitter retypes the listing and, worse, an unticked release
    checkbox silently drops a claim they already made."""
    client.force_login(submitter)

    body = client.get(reverse("submit:revise", args=[bounced.uuid])).content.decode()

    assert 'value="PowerEdge R750"' in body
    assert 'value="R750"' in body
    assert "Please add the BIOS version." in body
    # The AlmaLinux 9 box has to come back ticked.
    checked = body[body.index('name="release_support_9"') - 200:
                   body.index('name="release_support_9"') + 200]
    assert "checked" in checked


def test_revising_returns_the_same_row_to_the_queue(client, bounced, submitter):
    client.force_login(submitter)

    resp = client.post(
        reverse("submit:revise", args=[bounced.uuid]),
        _payload(bounced.listing.vendor, description="Now with BIOS 2.10.2"),
    )

    assert resp.status_code == 302
    bounced.refresh_from_db()
    assert bounced.status == Submission.STATUS_PENDING
    assert Submission.objects.count() == 1, "a second submission row was opened"
    bounced.listing.refresh_from_db()
    assert bounced.listing.description == "Now with BIOS 2.10.2"


def test_the_previous_decision_is_cleared(client, bounced, submitter, reviewer):
    """The row must read as awaiting a decision, not as carrying the old one."""
    client.force_login(submitter)
    assert bounced.reviewed_by == reviewer

    client.post(
        reverse("submit:revise", args=[bounced.uuid]),
        _payload(bounced.listing.vendor),
    )

    bounced.refresh_from_db()
    assert bounced.reviewed_by is None
    assert bounced.reviewed_at is None
    # Kept: it is what the reviewer asked for and they are about to check it was done.
    assert bounced.reviewer_notes == "Please add the BIOS version."


def test_reviewers_are_told_it_came_back(client, bounced, submitter, settings):
    settings.LUMINA_REVIEW_NOTIFY_EMAILS = ["reviewers@example.com"]
    client.force_login(submitter)

    client.post(
        reverse("submit:revise", args=[bounced.uuid]),
        _payload(bounced.listing.vendor),
    )

    # Deferred: the revision queues an event and the drainer mails the reviewers - no synchronous
    # send in the request. A revision re-enters the queue as an ordinary "awaiting review".
    from lumina.notifications import services
    assert mail.outbox == []
    services.deliver_pending()
    assert mail.outbox[0].to == ["reviewers@example.com"]


def test_the_listing_does_not_reappear_in_the_queue_twice(client, bounced, submitter):
    """The failure the old workaround produced: re-submitting from scratch left the
    bounced row *and* a fresh pending row for one machine."""
    client.force_login(submitter)

    client.post(
        reverse("submit:revise", args=[bounced.uuid]),
        _payload(bounced.listing.vendor),
    )

    open_rows = Submission.objects.filter(status__in=Submission.OPEN_STATUSES)
    assert open_rows.count() == 1
    assert System.objects.count() == 1


# --- releases on a revision -----------------------------------------------------


def test_unticking_a_release_removes_it(client, bounced, submitter):
    """A revision replaces rather than adds. A run's listing proposal is deliberately
    additive because a run is evidence; this is one person correcting their own
    unpublished claim, so unticking has to untick."""
    client.force_login(submitter)
    listing = bounced.listing
    assert set(listing.versions.values_list("release__major", flat=True)) == {9}

    client.post(reverse("submit:revise", args=[bounced.uuid]), _payload(
        listing.vendor,
        **{"release_support_9": "", "release_support_10": "1",
           "release_min_minor_10": "0"},
    ))

    assert set(listing.versions.values_list("release__major", flat=True)) == {10}
    bounced.refresh_from_db()
    assert set(bounced.cited_releases.values_list("major", flat=True)) == {10}


# ``test_correcting_a_minor_floor_lands`` stood here. A revision could lower a declared row's
# ``minimum_minor``, which mattered because ``_attach_release_versions`` is get_or_create and
# would otherwise leave the old floor in place. Hardware certifies per major now, so a row holds
# nothing a revision could correct beyond its own existence - covered by the tests above, which
# check that ticking adds and unticking removes a *declared* row.


def test_a_run_proven_release_is_left_alone(client, bounced, submitter):
    """The invariant that deleting the re-validation flow was about.

    Nothing may delete a proven row through a form, and nothing outside
    ``record_compatibility`` may mark one as proven. A reviewer can assign a run to a
    still-draft listing, so this is reachable rather than theoretical.
    """
    client.force_login(submitter)
    listing = bounced.listing
    proven = ListingVersion.objects.create(
        listing_system=listing,
        release=AlmaLinuxRelease.objects.get(major=8),
        source=ListingVersion.SOURCE_RUN,
    )

    # AlmaLinux 8 is not ticked, so the naive "replace everything" would delete it.
    client.post(reverse("submit:revise", args=[bounced.uuid]), _payload(listing.vendor))

    proven.refresh_from_db()
    assert proven.source == ListingVersion.SOURCE_RUN
    assert listing.versions.filter(release__major=8).exists()


# --- who may do it --------------------------------------------------------------


def test_somebody_elses_submission_is_a_404(client, bounced):
    """404 rather than 403: which submissions a user may revise is private, so "not
    yours" and "wrong status" must be indistinguishable from outside."""
    other = User.objects.create_user("nosy", password="pw")
    client.force_login(other)

    resp = client.get(reverse("submit:revise", args=[bounced.uuid]))

    assert resp.status_code == 404


def test_a_pending_submission_cannot_be_revised(client, bounced, submitter):
    """Only something a reviewer sent back. A pending row is already in the queue and
    editing it under a reviewer mid-decision is how two people disagree about what was
    approved."""
    bounced.status = Submission.STATUS_PENDING
    bounced.save(update_fields=["status"])
    client.force_login(submitter)

    resp = client.get(reverse("submit:revise", args=[bounced.uuid]))

    assert resp.status_code == 404


def test_an_approved_submission_cannot_be_revised(client, bounced, submitter, reviewer):
    bounced.status = Submission.STATUS_APPROVED
    bounced.save(update_fields=["status"])
    client.force_login(submitter)

    assert client.get(
        reverse("submit:revise", args=[bounced.uuid])
    ).status_code == 404


def test_anonymous_is_sent_to_log_in(client, bounced):
    # The fixture submits through the real form, so it leaves the client logged in.
    client.logout()

    resp = client.get(reverse("submit:revise", args=[bounced.uuid]))

    assert resp.status_code == 302
    assert "/submit/revise/" not in resp["Location"].split("?")[0]


def test_resubmit_refuses_from_the_wrong_state(bounced, reviewer):
    """The model-level guard, independent of the view."""
    bounced.status = Submission.STATUS_APPROVED

    with pytest.raises(ValueError) as exc:
        bounced.resubmit()

    assert "submission" in str(exc.value)
