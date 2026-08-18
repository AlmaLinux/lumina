"""Tests for the audit log.

Behavior pinned down:

- ``log_action(action, target, actor=..., before=..., after=..., ip=...)``
  creates an AuditLogEntry. ``target`` is any Django model instance; its
  content type and pk are captured.
- If ``actor``/``ip`` are omitted, they are pulled from the current request
  context (set by ``AuditContextMiddleware`` using a contextvar).
- When neither an explicit actor nor a request is available, actor is None
  (system/CLI action).
- ``AuditContextMiddleware`` extracts the real client IP preferring
  ``X-Forwarded-For`` (first hop) over ``REMOTE_ADDR`` - needed since
  production runs behind nginx.
- Entries are append-only: there's no public update path; attempting to save
  an existing entry after creation raises.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import RequestFactory

from lumina.audit.context import bind_request, clear_request
from lumina.audit.middleware import AuditContextMiddleware
from lumina.audit.models import AuditLogEntry
from lumina.audit.services import log_action
from lumina.vendors.models import Vendor

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture
def actor():
    return User.objects.create_user(username="reviewer")


@pytest.fixture
def target():
    return Vendor.objects.create(name="Target Vendor")


@pytest.fixture(autouse=True)
def _clear_context():
    clear_request()
    yield
    clear_request()


class LogActionTests:
    def test_creates_entry_with_content_type_and_pk(self, actor, target):
        entry = log_action("vendor.verify", target=target, actor=actor)
        assert entry.action == "vendor.verify"
        assert entry.target_content_type == ContentType.objects.get_for_model(target)
        assert entry.target_id == target.pk
        assert entry.actor == actor

    def test_captures_before_and_after_json(self, actor, target):
        entry = log_action(
            "vendor.verify",
            target=target,
            actor=actor,
            before={"verified": False},
            after={"verified": True},
        )
        assert entry.before == {"verified": False}
        assert entry.after == {"verified": True}

    def test_actor_falls_back_to_request_context(self, actor, target):
        bind_request(actor=actor, ip="10.0.0.7")
        entry = log_action("vendor.verify", target=target)
        assert entry.actor == actor
        assert entry.ip == "10.0.0.7"

    def test_no_actor_and_no_request_means_system_action(self, target):
        entry = log_action("system.gc", target=target)
        assert entry.actor is None
        assert entry.ip is None


class AuditContextMiddlewareTests:
    def _request(self, **meta) -> object:
        return RequestFactory().get("/", **meta)

    def test_stores_actor_and_client_ip_from_remote_addr(self, actor, target):
        req = self._request(REMOTE_ADDR="192.0.2.1")
        req.user = actor

        captured: dict[str, AuditLogEntry] = {}

        def view(r):
            captured["entry"] = log_action("vendor.verify", target=target)
            return "ok"

        AuditContextMiddleware(view)(req)
        assert captured["entry"].actor == actor
        assert captured["entry"].ip == "192.0.2.1"

    def test_prefers_x_forwarded_for_first_hop(self, actor, target):
        req = self._request(
            REMOTE_ADDR="10.0.0.1",
            HTTP_X_FORWARDED_FOR="203.0.113.5, 10.0.0.1",
        )
        req.user = actor
        captured: dict[str, AuditLogEntry] = {}

        def view(r):
            captured["entry"] = log_action("vendor.verify", target=target)
            return "ok"

        AuditContextMiddleware(view)(req)
        assert captured["entry"].ip == "203.0.113.5"

    def test_anonymous_request_leaves_actor_none(self, target):
        from django.contrib.auth.models import AnonymousUser
        req = self._request(REMOTE_ADDR="10.0.0.1")
        req.user = AnonymousUser()
        captured: dict[str, AuditLogEntry] = {}

        def view(r):
            captured["entry"] = log_action("system.something", target=target)
            return "ok"

        AuditContextMiddleware(view)(req)
        assert captured["entry"].actor is None

    def test_context_cleared_after_request(self, actor, target):
        req = self._request(REMOTE_ADDR="10.0.0.1")
        req.user = actor
        AuditContextMiddleware(lambda r: "ok")(req)
        # A subsequent non-request log should not see the stale actor.
        entry = log_action("system.gc", target=target)
        assert entry.actor is None
        assert entry.ip is None


class AppendOnlyTests:
    def test_saving_existing_entry_raises(self, actor, target):
        entry = log_action("vendor.verify", target=target, actor=actor)
        entry.action = "vendor.unverify"
        with pytest.raises(ValueError):
            entry.save()
