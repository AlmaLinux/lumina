"""Pytest shared fixtures."""
import uuid

import pytest


@pytest.fixture(autouse=True)
def _media_root(tmp_path, settings):
    """Ensure every test gets an isolated MEDIA_ROOT so uploads don't bleed."""
    settings.MEDIA_ROOT = str(tmp_path / "media")


@pytest.fixture(autouse=True)
def _isolated_cache(settings):
    """Give every test its own empty cache.

    DRF throttle counters live in the cache, so a shared one makes the suite
    history-dependent: run it a few times inside an hour and the ingest tests
    start failing with 429 because the 30/hour budget is spent. That happens
    whenever DJANGO_SETTINGS_MODULE points at a real deployment's settings
    instead of settings.test - the devstack container sets exactly that in its
    environment, which overrides the value in pyproject.

    A per-test locmem cache rather than clearing the configured one: against a
    real Valkey, ``cache.clear()`` would also drop the sessions of anyone using
    that deployment, since sessions are cache-backed.
    """
    settings.CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": f"test-{uuid.uuid4()}",
        }
    }
