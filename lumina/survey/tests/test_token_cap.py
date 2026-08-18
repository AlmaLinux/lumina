"""A survey-token grant lifts the 30-day token ceiling for that account only."""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from lumina.accounts.forms import ApiTokenCreateForm
from lumina.accounts.models import ApiToken
from lumina.survey import services
from lumina.survey.models import SurveyTokenGrant

pytestmark = pytest.mark.django_db
User = get_user_model()
DAY = 60 * 60 * 24


def _form(user, ttl):
    return ApiTokenCreateForm(
        {"name": "t", "scopes": [ApiToken.SCOPE_SUBMIT], "ttl_seconds": ttl},
        user=user,
    )


def test_ungranted_user_cannot_choose_more_than_30_days():
    user = User.objects.create_user(username="u", password="x")
    assert not services.can_issue_long_tokens(user)
    assert not _form(user, 366 * DAY).is_valid()   # a year is not on offer
    assert _form(user, 30 * DAY).is_valid()


def test_granted_user_can_mint_a_year_long_token():
    user = User.objects.create_user(username="g", password="x")
    SurveyTokenGrant.objects.create(user=user)
    assert services.can_issue_long_tokens(user)

    form = _form(user, 366 * DAY)
    assert form.is_valid(), form.errors
    assert form.cleaned_data["ttl_seconds"] == 366 * DAY


def test_grant_max_ttl_narrows_the_cap():
    user = User.objects.create_user(username="g2", password="x")
    SurveyTokenGrant.objects.create(user=user, max_ttl_seconds=90 * DAY)

    assert not _form(user, 366 * DAY).is_valid()   # a year exceeds their 90-day grant
    assert _form(user, 90 * DAY).is_valid()


def test_revoked_grant_returns_to_the_default_cap():
    from django.utils import timezone
    user = User.objects.create_user(username="g3", password="x")
    SurveyTokenGrant.objects.create(user=user, revoked_at=timezone.now())

    assert not services.can_issue_long_tokens(user)
    assert not _form(user, 366 * DAY).is_valid()
