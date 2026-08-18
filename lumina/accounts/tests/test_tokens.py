"""Tests for the ApiToken model.

Behavior pinned down:

- ``ApiToken.issue`` returns ``(instance, raw_token)``; the raw value is
  only available at creation, stored as a SHA-256 hash.
- Tokens default to ``read`` scope and TTL from ``settings.LUMINA_API_TOKEN_TTL_SECONDS``.
- ``ApiToken.resolve(raw)`` returns the active token or ``None`` for
  missing/expired/revoked tokens.
- ``revoke()`` sets ``revoked_at`` and subsequent ``resolve`` returns None.
- ``has_scope`` membership test.
- ``issue(ttl_seconds=0)`` means non-expiring.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from lumina.accounts.models import ApiToken

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture
def user():
    return User.objects.create_user(username="bob")


class IssueTests:
    def test_returns_instance_and_raw(self, user):
        token, raw = ApiToken.issue(user=user, name="laptop")
        assert isinstance(token, ApiToken)
        assert isinstance(raw, str) and len(raw) >= 32

    def test_raw_not_stored(self, user):
        token, raw = ApiToken.issue(user=user, name="laptop")
        token.refresh_from_db()
        assert raw not in (token.token_hash or "")
        assert len(token.token_hash) == 64  # sha256 hex

    def test_explicit_scopes(self, user):
        token, _ = ApiToken.issue(
            user=user, name="ci", scopes=[ApiToken.SCOPE_READ, ApiToken.SCOPE_SUBMIT]
        )
        assert token.has_scope(ApiToken.SCOPE_SUBMIT)
        assert token.has_scope(ApiToken.SCOPE_READ)

    def test_has_scope_negative(self, user):
        token, _ = ApiToken.issue(user=user, name="laptop")
        assert not token.has_scope(ApiToken.SCOPE_SUBMIT)

    def test_default_ttl_from_settings(self, user, settings):
        settings.LUMINA_API_TOKEN_TTL_SECONDS = 3600
        before = timezone.now()
        token, _ = ApiToken.issue(user=user, name="laptop")
        assert token.expires_at is not None
        assert token.expires_at >= before + timedelta(seconds=3500)

    def test_ttl_zero_means_no_expiry(self, user):
        token, _ = ApiToken.issue(user=user, name="laptop", ttl_seconds=0)
        assert token.expires_at is None


class ResolveTests:
    def test_resolves_to_owning_user(self, user):
        token, raw = ApiToken.issue(user=user, name="laptop")
        resolved = ApiToken.resolve(raw)
        assert resolved is not None
        assert resolved.user == user
        assert resolved.pk == token.pk

    def test_unknown_returns_none(self):
        assert ApiToken.resolve("not-a-real-token") is None

    def test_expired_returns_none(self, user):
        token, raw = ApiToken.issue(user=user, name="short", ttl_seconds=60)
        ApiToken.objects.filter(pk=token.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        assert ApiToken.resolve(raw) is None

    def test_revoked_returns_none(self, user):
        token, raw = ApiToken.issue(user=user, name="laptop")
        token.revoke()
        assert ApiToken.resolve(raw) is None


class RevokeTests:
    def test_revoke_sets_timestamp(self, user):
        token, _ = ApiToken.issue(user=user, name="laptop")
        assert token.revoked_at is None
        token.revoke()
        token.refresh_from_db()
        assert token.revoked_at is not None

    def test_is_active_after_revoke_is_false(self, user):
        token, _ = ApiToken.issue(user=user, name="laptop")
        token.revoke()
        assert token.is_active() is False
