"""Requesting the long-token capability, and a reviewer deciding it."""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.urls import reverse

from lumina.notifications.models import NotificationEvent
from lumina.survey import services
from lumina.survey.models import SurveyTokenGrant, SurveyTokenRequest

pytestmark = pytest.mark.django_db
User = get_user_model()


def _reviewer():
    rev = User.objects.create_user(username="rev", password="x")
    rev.groups.add(Group.objects.get_or_create(name="reviewer")[0])
    return rev


def test_request_long_token_creates_and_notifies_reviewers():
    user = User.objects.create_user(username="u", password="x")
    req = services.request_long_token(requester=user, justification="200 build servers.")

    assert req.status == SurveyTokenRequest.STATUS_PENDING
    assert NotificationEvent.objects.filter(
        event_key="survey_token_request.submitted"
    ).exists()


def test_one_open_request_per_user():
    user = User.objects.create_user(username="u", password="x")
    services.request_long_token(requester=user, justification="a")
    with pytest.raises(ValueError, match="already have an open"):
        services.request_long_token(requester=user, justification="b")


def test_request_page_renders_and_submits(client):
    user = User.objects.create_user(username="u2", password="x")
    client.force_login(user)
    assert client.get(reverse("survey:request_token")).status_code == 200

    resp = client.post(reverse("survey:request_token"), {"justification": "fleet of 50"})
    assert resp.status_code == 302
    assert SurveyTokenRequest.objects.filter(requester=user).exists()


def test_reviewer_approval_grants_the_capability(client):
    user = User.objects.create_user(username="fleet", password="x")
    req = services.request_long_token(requester=user, justification="fleet")

    client.force_login(_reviewer())
    resp = client.post(reverse("review:survey_token_approve", args=[req.pk]))

    assert resp.status_code == 302
    req.refresh_from_db()
    assert req.status == SurveyTokenRequest.STATUS_APPROVED
    assert SurveyTokenGrant.objects.filter(user=user, revoked_at__isnull=True).exists()
    assert services.can_issue_long_tokens(user)
    assert NotificationEvent.objects.filter(
        event_key="survey_token_request.decided"
    ).exists()


def test_reviewer_rejection_grants_nothing(client):
    user = User.objects.create_user(username="fleet2", password="x")
    req = services.request_long_token(requester=user, justification="fleet")

    client.force_login(_reviewer())
    resp = client.post(reverse("review:survey_token_reject", args=[req.pk]), {"reason": "no"})

    assert resp.status_code == 302
    req.refresh_from_db()
    assert req.status == SurveyTokenRequest.STATUS_REJECTED
    assert not SurveyTokenGrant.objects.filter(user=user).exists()


def test_pending_request_appears_in_the_review_queue(client):
    user = User.objects.create_user(username="q", password="x")
    services.request_long_token(requester=user, justification="fleet of 99 machines")

    client.force_login(_reviewer())
    resp = client.get(reverse("review:queue"))

    assert resp.status_code == 200
    assert b"Survey tokens" in resp.content            # the conditional tab appears
    assert b"fleet of 99 machines" in resp.content     # the justification is shown
