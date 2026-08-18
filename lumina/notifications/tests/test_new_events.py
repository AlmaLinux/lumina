"""The transitions that used to notify nobody.

Each of these is a decision or a state change somebody was waiting on, audited since the day it was
written and silent until now: a proposer never heard what came of their edit, a submitter never
heard their run had gone public, and a survey contributor never heard that their submission had
been dismissed.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import Group, User
from django.urls import reverse

from lumina.notifications.models import NotificationEvent
from lumina.survey.models import SurveySubmission

pytestmark = pytest.mark.django_db


@pytest.fixture
def reviewer(client):
    user = User.objects.create_user("ne-rev", password="pw", email="nerev@example.com")
    user.groups.add(Group.objects.get_or_create(name="reviewer")[0])
    client.force_login(user)
    return user


@pytest.fixture
def proposer():
    return User.objects.create_user("ne-prop", email="neprop@example.com")


def _keys() -> list[str]:
    return list(NotificationEvent.objects.values_list("event_key", flat=True))


# --- an edit proposal gets an answer ---------------------------------------------


@pytest.fixture
def software_proposal(proposer):
    from lumina.software.models import Software, SoftwareEditProposal
    from lumina.vendors.models import Vendor

    vendor = Vendor.objects.create(name="Vaultwise")
    product = Software.objects.create(name="Archive", vendor=vendor, owner_vendor=vendor)
    return SoftwareEditProposal.objects.create(
        software=product, proposed_by=proposer, name="Archive Pro",
    )


@pytest.mark.parametrize("view,data", [
    ("review:software_edit_approve", {}),
    ("review:software_edit_reject", {"reason": "not the name"}),
])
def test_a_software_edit_decision_reaches_the_proposer(client, reviewer, software_proposal,
                                                       view, data):
    client.post(reverse(view, args=[software_proposal.pk]), data, follow=True)

    assert "proposal.decided" in _keys()


@pytest.fixture
def listing_proposal(proposer):
    from lumina.hardware.models import ListingEditProposal, System
    from lumina.vendors.models import Vendor

    vendor = Vendor.objects.create(name="Boardworks")
    listing = System.objects.create(
        name="PowerEdge R760", vendor=vendor, model_number="R760",
    )
    return ListingEditProposal.objects.create(
        listing_system=listing, proposed_by=proposer, name="PowerEdge R760xa",
    )


@pytest.mark.parametrize("view,data", [
    ("review:listing_edit_approve", {}),
    ("review:listing_edit_reject", {"reason": "wrong model"}),
])
def test_a_listing_edit_decision_reaches_the_proposer(client, reviewer, listing_proposal,
                                                      view, data):
    client.post(reverse(view, args=[listing_proposal.pk]), data, follow=True)

    assert "proposal.decided" in _keys()


def test_the_proposal_event_is_addressed_at_whoever_proposed_it(software_proposal):
    """``proposed_by`` rather than ``submitter``: an edit is a suggestion, and the person owed the
    answer is the one who made it."""
    from lumina.notifications.services import _users_for

    assert _users_for("submitter", software_proposal) == [software_proposal.proposed_by]


# --- a run comes off hold ---------------------------------------------------------


def test_publishing_a_held_run_tells_its_submitter():
    """The only transition that happens on a timer rather than from somebody's request, so there
    is no page the submitter was watching when it occurred."""
    from lumina.results import ingest
    from lumina.results.models import TestRun
    from lumina.results.services import release_held_run
    from lumina.results.tests import factories as f

    submitter = User.objects.create_user("ne-sub", email="nesub@example.com")
    run = ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(f.build_bundle(f.make_report())),
        source="api",
    )
    TestRun.objects.filter(pk=run.pk).update(status=TestRun.STATUS_APPROVED, published_at=None)
    run.refresh_from_db()
    NotificationEvent.objects.all().delete()

    assert release_held_run(run) is True
    assert _keys() == ["run.published"]


def test_a_run_that_was_already_published_says_nothing_again():
    """``release_held_run`` is idempotent and the notification has to be too, or the drainer mails
    somebody every time the timer runs."""
    from django.utils import timezone

    from lumina.results import ingest
    from lumina.results.models import TestRun
    from lumina.results.services import release_held_run
    from lumina.results.tests import factories as f

    submitter = User.objects.create_user("ne-sub2", email="nesub2@example.com")
    run = ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(f.build_bundle(f.make_report())),
        source="api",
    )
    TestRun.objects.filter(pk=run.pk).update(
        status=TestRun.STATUS_APPROVED, published_at=timezone.now())
    run.refresh_from_db()
    NotificationEvent.objects.all().delete()

    assert release_held_run(run) is False
    assert _keys() == []


# --- a survey submission is moderated ----------------------------------------------


@pytest.mark.parametrize("view", [
    "review:survey_submission_accept", "review:survey_submission_dismiss",
])
def test_moderating_a_survey_submission_tells_its_submitter(client, reviewer, view):
    submitter = User.objects.create_user("ne-surv", email="nesurv@example.com")
    sub = SurveySubmission.objects.create(
        origin=SurveySubmission.ORIGIN_SURVEY, trust_tier=SurveySubmission.TIER_VERIFIED,
        identity_hash="h", cpu_vendor="GenuineIntel", submitter=submitter,
    )

    client.post(reverse(view, args=[sub.pk]), follow=True)

    assert "survey_submission.decided" in _keys()
