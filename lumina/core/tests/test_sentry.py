"""Sentry is opt-in: without a DSN it must be entirely inert (tests and CI never phone home)."""
from __future__ import annotations

from django.conf import settings


def test_sentry_is_disabled_without_a_dsn():
    # ``lumina.settings.base`` only calls ``sentry_sdk.init`` when SENTRY_DSN is non-empty; the test
    # settings inherit base with no DSN set, so nothing is initialized and no events can be sent.
    assert settings.SENTRY_DSN == ""
