"""The survey's foundation: an append-only submission and a review-gated token grant."""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from lumina.survey.models import (
    SurveySubmission,
    SurveyTokenGrant,
    SurveyTokenRequest,
)

pytestmark = pytest.mark.django_db
User = get_user_model()


def _user(username: str = "alice") -> User:
    return User.objects.create_user(username=username, password="x")


def test_submission_raw_data_is_append_only():
    sub = SurveySubmission.objects.create(
        origin=SurveySubmission.ORIGIN_SURVEY,
        trust_tier=SurveySubmission.TIER_VERIFIED,
        inventory={"summary": {}},
    )
    sub.cpu_model = "tampered"
    with pytest.raises(ValueError, match="append-only"):
        sub.save()


def test_operational_columns_may_still_change():
    sub = SurveySubmission.objects.create(
        origin=SurveySubmission.ORIGIN_SURVEY,
        trust_tier=SurveySubmission.TIER_VERIFIED,
    )
    sub.review_state = SurveySubmission.REVIEW_ACCEPTED
    sub.save(update_fields=["review_state"])  # in the mutable allowlist
    sub.refresh_from_db()
    assert sub.review_state == SurveySubmission.REVIEW_ACCEPTED


def test_a_raw_column_cannot_ride_along_with_a_mutable_one():
    sub = SurveySubmission.objects.create(
        origin=SurveySubmission.ORIGIN_SURVEY,
        trust_tier=SurveySubmission.TIER_VERIFIED,
    )
    sub.review_state = SurveySubmission.REVIEW_ACCEPTED
    sub.cpu_model = "tampered"
    with pytest.raises(ValueError, match="append-only"):
        sub.save(update_fields=["review_state", "cpu_model"])


def test_countable_excludes_vms_and_dismissed():
    common = dict(
        origin=SurveySubmission.ORIGIN_SURVEY,
        trust_tier=SurveySubmission.TIER_VERIFIED,
    )
    keep = SurveySubmission.objects.create(**common)
    SurveySubmission.objects.create(virtual=True, **common)
    dismissed = SurveySubmission.objects.create(**common)
    dismissed.review_state = SurveySubmission.REVIEW_DISMISSED
    dismissed.save(update_fields=["review_state"])

    countable = list(SurveySubmission.objects.countable())
    assert countable == [keep]


def test_approving_a_request_grants_the_capability():
    requester = _user("bob")
    reviewer = _user("rev")
    req = SurveyTokenRequest.objects.create(
        requester=requester, justification="Fleet of 200 build servers."
    )

    grant = req.approve(by=reviewer)

    assert req.status == SurveyTokenRequest.STATUS_APPROVED
    assert req.reviewed_by == reviewer
    assert grant.granted_by == reviewer
    assert SurveyTokenGrant.objects.get(user=requester).is_active()


def test_rejecting_a_request_grants_nothing():
    requester = _user("carol")
    reviewer = _user("rev")
    req = SurveyTokenRequest.objects.create(requester=requester, justification="...")

    req.reject(by=reviewer, reason="Could not verify.")

    assert req.status == SurveyTokenRequest.STATUS_REJECTED
    assert not SurveyTokenGrant.objects.filter(user=requester).exists()
