"""Audit logging service.

Callers invoke ``log_action`` at the point of each audited operation. Any
of ``actor``/``ip`` omitted from the call fall back to the request-scoped
context bound by AuditContextMiddleware.
"""
from __future__ import annotations

from typing import Any

from django.contrib.contenttypes.models import ContentType
from django.db import models

from lumina.audit.context import current
from lumina.audit.models import AuditLogEntry


def log_action(
    action: str,
    *,
    target: models.Model,
    actor: Any | None = None,
    ip: str | None = None,
    before: dict | None = None,
    after: dict | None = None,
    notes: str = "",
) -> AuditLogEntry:
    """Record an audit entry. ``action`` is a dotted name like ``submission.approve``."""
    ctx = current()
    if actor is None and ctx is not None:
        actor = ctx.actor
    if ip is None:
        ip = ctx.ip if ctx is not None else ""
    return AuditLogEntry.objects.create(
        action=action,
        actor=actor,
        ip=ip or None,
        target_content_type=ContentType.objects.get_for_model(target),
        target_id=target.pk,
        before=before,
        after=after,
        notes=notes,
    )
