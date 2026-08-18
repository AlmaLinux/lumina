"""Outbox-backed notifications: events queued in-transaction, delivered out of band.

A state change that someone needs to act on writes one ``NotificationEvent`` inside its own DB
transaction (so it commits atomically with the change and is discarded on rollback), and returns.
The ``deliver_notifications`` management command, run every minute by a systemd timer, fans each
event out to its audience's email addresses and any subscribed webhook endpoints and delivers them -
all the slow SMTP/HTTP work out of the request path. Nothing here blocks a submission or a render.
"""
from __future__ import annotations

import secrets
from typing import override

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models


class WebhookEndpoint(models.Model):
    """An admin-registered URL that receives HMAC-signed POSTs for the event types it subscribes to.

    Global, not per-vendor: it fires for every matching event, whoever it is about. The signing
    ``secret`` is stored retrievably - unlike an API token, a webhook secret must be handed back to
    the receiver so it can verify the signature - and is shown in the admin.
    """

    KIND_GENERIC = "generic"
    KIND_MATTERMOST = "mattermost"
    KIND_CHOICES = [
        (KIND_GENERIC, "Generic (HMAC-signed event JSON)"),
        (KIND_MATTERMOST, "Mattermost incoming webhook"),
    ]

    name = models.CharField(max_length=120, help_text="A label for this endpoint.")
    url = models.URLField(help_text="Where the POST is sent.")
    kind = models.CharField(
        max_length=16, choices=KIND_CHOICES, default=KIND_GENERIC,
        help_text="Generic posts the signed event JSON. Mattermost posts a chat message to an "
                  "incoming-webhook URL - no signature, the unguessable URL is the secret.",
    )
    secret = models.CharField(
        max_length=64, blank=True,
        help_text="Generic only: the shared secret the receiver verifies the X-Lumina-Signature "
                  "HMAC against. Generated on first save if left blank; unused for Mattermost.",
    )
    event_keys = models.JSONField(
        default=list,
        help_text='Event keys this endpoint receives, e.g. ["run.submitted", "submission.created"]. '
                  "See the notification event registry for the full list.",
    )
    enabled = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    last_delivery_at = models.DateTimeField(null=True, blank=True)
    last_status = models.CharField(max_length=40, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.name

    @override
    def save(self, *args, **kwargs):
        # Only the generic kind signs, so only it needs a secret; a Mattermost URL is its own secret.
        if self.kind == self.KIND_GENERIC and not self.secret:
            self.secret = secrets.token_urlsafe(32)
        super().save(*args, **kwargs)

    def wants(self, event_key: str) -> bool:
        return self.enabled and event_key in (self.event_keys or [])


class NotificationEvent(models.Model):
    """One occurrence of a notifiable event - the outbox row written in-transaction by ``emit``.

    Minimal on purpose: it names the event and points at the object, and the drainer renders content
    from the live target at delivery time. ``processed_at`` is set once the event has been fanned out
    into per-recipient ``NotificationDelivery`` rows, so a second drain does not fan it out twice.
    """

    event_key = models.CharField(max_length=60, db_index=True)
    target_content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE, related_name="+")
    target_id = models.PositiveBigIntegerField()
    target = GenericForeignKey("target_content_type", "target_id")
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    processed_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [models.Index(fields=["target_content_type", "target_id"])]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.event_key} #{self.pk}"


class NotificationDelivery(models.Model):
    """One attempt-tracked delivery of an event to one destination - an email address or an endpoint.

    Concurrency- and retry-safe without holding a lock across the slow send: the drainer claims a due
    delivery with a compare-and-set ``UPDATE`` (bumping ``attempts`` and pushing ``next_attempt_at``
    into the future as a lease), sends outside any transaction, then records the outcome.
    """

    CHANNEL_EMAIL = "email"
    CHANNEL_WEBHOOK = "webhook"
    CHANNEL_CHOICES = [(CHANNEL_EMAIL, "Email"), (CHANNEL_WEBHOOK, "Webhook")]

    STATUS_PENDING = "pending"
    STATUS_SENT = "sent"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"), (STATUS_SENT, "Sent"), (STATUS_FAILED, "Failed"),
    ]

    event = models.ForeignKey(NotificationEvent, on_delete=models.CASCADE, related_name="deliveries")
    channel = models.CharField(max_length=10, choices=CHANNEL_CHOICES)
    email = models.EmailField(blank=True)
    endpoint = models.ForeignKey(
        WebhookEndpoint, on_delete=models.CASCADE, null=True, blank=True, related_name="deliveries",
    )
    status = models.CharField(
        max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True,
    )
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at"]
        # Django would otherwise pluralize the model name as "notification deliverys".
        verbose_name_plural = "notification deliveries"
        constraints = [
            # Fan-out uses get_or_create under an event-level lock, so this is a backstop. Fully
            # enforced for webhooks; email rows (endpoint IS NULL, distinct under SQL null semantics)
            # lean on that lock + get_or_create instead.
            models.UniqueConstraint(
                fields=["event", "channel", "email", "endpoint"],
                name="notification_delivery_unique_destination",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.channel} -> {self.email or self.endpoint} ({self.status})"
