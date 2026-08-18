"""Tests for the DRF ApiTokenAuthentication class.

Behavior pinned down:

- No Authorization header → authenticate returns None (falls through to
  other auth classes / anonymous).
- Malformed Bearer header → AuthenticationFailed.
- Unknown/expired/revoked token → AuthenticationFailed.
- Valid token → ``(user, token)`` returned; ``last_used_at`` is updated.
- Wrong scheme (e.g. ``Token xyz``) → None (not our concern).
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from rest_framework import exceptions
from rest_framework.test import APIRequestFactory

from lumina.accounts.auth import ApiTokenAuthentication
from lumina.accounts.models import ApiToken

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture
def factory():
    return APIRequestFactory()


@pytest.fixture
def user():
    return User.objects.create_user(username="dave")


class ApiTokenAuthenticationTests:
    def test_no_header_returns_none(self, factory):
        req = factory.get("/api/v1/")
        assert ApiTokenAuthentication().authenticate(req) is None

    def test_non_bearer_scheme_returns_none(self, factory):
        req = factory.get("/api/v1/", HTTP_AUTHORIZATION="Token abc")
        assert ApiTokenAuthentication().authenticate(req) is None

    def test_bearer_without_token_raises(self, factory):
        req = factory.get("/api/v1/", HTTP_AUTHORIZATION="Bearer")
        with pytest.raises(exceptions.AuthenticationFailed):
            ApiTokenAuthentication().authenticate(req)

    def test_invalid_token_raises(self, factory):
        req = factory.get("/api/v1/", HTTP_AUTHORIZATION="Bearer not-a-real-token")
        with pytest.raises(exceptions.AuthenticationFailed):
            ApiTokenAuthentication().authenticate(req)

    def test_valid_token_returns_user_and_token(self, factory, user):
        token, raw = ApiToken.issue(user=user, name="ci")
        req = factory.get("/api/v1/", HTTP_AUTHORIZATION=f"Bearer {raw}")
        result = ApiTokenAuthentication().authenticate(req)
        assert result is not None
        authed_user, authed_token = result
        assert authed_user == user
        assert authed_token.pk == token.pk

    def test_valid_token_updates_last_used(self, factory, user):
        token, raw = ApiToken.issue(user=user, name="ci")
        assert token.last_used_at is None
        req = factory.get("/api/v1/", HTTP_AUTHORIZATION=f"Bearer {raw}")
        ApiTokenAuthentication().authenticate(req)
        token.refresh_from_db()
        assert token.last_used_at is not None

    def test_revoked_token_raises(self, factory, user):
        token, raw = ApiToken.issue(user=user, name="ci")
        token.revoke()
        req = factory.get("/api/v1/", HTTP_AUTHORIZATION=f"Bearer {raw}")
        with pytest.raises(exceptions.AuthenticationFailed):
            ApiTokenAuthentication().authenticate(req)
