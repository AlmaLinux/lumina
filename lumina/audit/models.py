"""Append-only audit log.

Entries capture who did what to which object, with optional JSON snapshots
of before/after state. Rows are intentionally immutable after creation: the
log exists so reviewers can be held accountable, so silent edits must be
impossible from within the application code path.
"""
from __future__ import annotations

from typing import override

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models


class AuditLogEntry(models.Model):
    action = models.CharField(
        max_length=80,
        db_index=True,
        help_text="Dotted action name, e.g. 'submission.approve'.",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    ip = models.GenericIPAddressField(null=True, blank=True)
    target_content_type = models.ForeignKey(
        ContentType, on_delete=models.CASCADE, related_name="+"
    )
    target_id = models.PositiveBigIntegerField()
    target = GenericForeignKey("target_content_type", "target_id")

    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)
    notes = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["target_content_type", "target_id"]),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.action} by {self.actor or 'system'} @ {self.created_at:%Y-%m-%d %H:%M}"

    @override
    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValueError("AuditLogEntry is append-only; cannot update after creation.")
        super().save(*args, **kwargs)
