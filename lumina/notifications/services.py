"""Enqueue events (in-transaction) and deliver them (out of band).

``emit`` is what the app calls: one cheap INSERT. ``deliver_pending`` is what the
``deliver_notifications`` command calls: it fans each queued event out to its audience's emails and
subscribed webhook endpoints, then delivers the due ones with retry/backoff. Delivery never holds a
DB lock across the slow SMTP/HTTP call - a due delivery is claimed with a compare-and-set ``UPDATE``
(portable across SQLite and MariaDB), sent, then its outcome recorded.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import urllib.error
import urllib.request
from datetime import timedelta

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.mail import send_mail
from django.db import connection, models, transaction
from django.template.loader import render_to_string
from django.utils import timezone

from lumina.notifications import events
from lumina.notifications.models import (
    NotificationDelivery,
    NotificationEvent,
    WebhookEndpoint,
)

LEASE_SECONDS = 120  # how long a claimed-but-unfinished delivery is left alone before a retry


def _max_attempts() -> int:
    return int(getattr(settings, "LUMINA_NOTIFY_MAX_ATTEMPTS", 5))


def _webhook_timeout() -> int:
    return int(getattr(settings, "LUMINA_WEBHOOK_TIMEOUT_SECONDS", 10))


def _base_url() -> str:
    return getattr(settings, "LUMINA_SITE_BASE_URL", "").rstrip("/")


def _backoff(attempts: int) -> timedelta:
    return timedelta(minutes=min(2 ** attempts, 60))


# --- enqueue --------------------------------------------------------------------


def emit(event_key: str, *, target, actor=None) -> NotificationEvent | None:
    """Enqueue a notification event: one in-transaction INSERT.

    The slow work (recipient resolution, email, webhooks) happens later in ``deliver_notifications``,
    so this never blocks the request or the transaction it rides in - and if that transaction rolls
    back, the event is discarded with it. A no-op when notifications, or this event key, are disabled.
    """
    if not getattr(settings, "LUMINA_NOTIFICATIONS_ENABLED", True):
        return None
    if event_key in set(getattr(settings, "LUMINA_NOTIFY_DISABLED_EVENTS", [])):
        return None
    if events.get(event_key) is None:
        raise ValueError(f"Unknown notification event key: {event_key!r}")
    return NotificationEvent.objects.create(
        event_key=event_key,
        target_content_type=ContentType.objects.get_for_model(target),
        target_id=target.pk,
        actor=actor,
    )


# --- drain ----------------------------------------------------------------------


def deliver_pending(*, max_events: int = 500, max_deliveries: int = 500) -> dict:
    """Fan out unprocessed events, then attempt due deliveries. Idempotent and overlap-safe."""
    fanned = _fan_out_batch(max_events)
    attempted = _attempt_batch(max_deliveries)
    return {"fanned_out": fanned, "attempted": attempted}


def _fan_out_batch(limit: int) -> int:
    pks = list(
        NotificationEvent.objects.filter(processed_at__isnull=True)
        .order_by("created_at").values_list("pk", flat=True)[:limit]
    )
    processed = 0
    for pk in pks:
        with transaction.atomic():
            qs = NotificationEvent.objects.filter(pk=pk, processed_at__isnull=True)
            # Lock the event so two overlapping drains cannot both fan it out; skip_locked is
            # unsupported on SQLite (single-writer anyway), so only ask for it where it works.
            if connection.features.has_select_for_update_skip_locked:
                qs = qs.select_for_update(skip_locked=True)
            event = qs.first()
            if event is None:
                continue
            _fan_out(event)
            event.processed_at = timezone.now()
            event.save(update_fields=["processed_at"])
            processed += 1
    return processed


def _fan_out(event: NotificationEvent) -> None:
    ev = events.get(event.event_key)
    target = event.target
    # Unknown event key or a target deleted before delivery: nothing to send, but the event is still
    # marked processed by the caller so it stops being reconsidered.
    if ev is None or target is None:
        return
    now = timezone.now()
    for email in _recipients(ev, target):
        NotificationDelivery.objects.get_or_create(
            event=event, channel=NotificationDelivery.CHANNEL_EMAIL, email=email, endpoint=None,
            defaults={"next_attempt_at": now},
        )
    if ev.webhookable:
        for endpoint in _endpoints_for(event.event_key):
            NotificationDelivery.objects.get_or_create(
                event=event, channel=NotificationDelivery.CHANNEL_WEBHOOK, email="", endpoint=endpoint,
                defaults={"next_attempt_at": now},
            )


def _attempt_batch(limit: int) -> int:
    now = timezone.now()
    pks = list(
        NotificationDelivery.objects.filter(status=NotificationDelivery.STATUS_PENDING)
        .filter(models.Q(next_attempt_at__isnull=True) | models.Q(next_attempt_at__lte=now))
        .order_by("created_at").values_list("pk", flat=True)[:limit]
    )
    sent = 0
    for pk in pks:
        if _attempt(pk):
            sent += 1
    return sent


def _attempt(pk: int) -> bool:
    now = timezone.now()
    # Compare-and-set claim: only one drain wins, and we bump attempts + lease next_attempt_at into
    # the future so a crash mid-send is retried later rather than wedging the row.
    claimed = (
        NotificationDelivery.objects.filter(pk=pk, status=NotificationDelivery.STATUS_PENDING)
        .filter(models.Q(next_attempt_at__isnull=True) | models.Q(next_attempt_at__lte=now))
        .update(next_attempt_at=now + timedelta(seconds=LEASE_SECONDS), attempts=models.F("attempts") + 1)
    )
    if not claimed:
        return False
    delivery = NotificationDelivery.objects.select_related(
        "event", "event__target_content_type", "event__actor", "endpoint"
    ).get(pk=pk)
    try:
        if delivery.channel == NotificationDelivery.CHANNEL_EMAIL:
            _send_email(delivery)
        else:
            _send_webhook(delivery)
    except Exception as exc:  # noqa: BLE001 - any delivery error becomes a retry or a give-up
        _record_failure(delivery, f"{type(exc).__name__}: {exc}")
        return False
    _record_success(delivery)
    return True


def _record_success(delivery: NotificationDelivery) -> None:
    now = timezone.now()
    delivery.status = NotificationDelivery.STATUS_SENT
    delivery.sent_at = now
    delivery.last_error = ""
    delivery.next_attempt_at = None
    delivery.save(update_fields=["status", "sent_at", "last_error", "next_attempt_at"])
    if delivery.endpoint_id:
        WebhookEndpoint.objects.filter(pk=delivery.endpoint_id).update(
            last_delivery_at=now, last_status="ok",
        )


def _record_failure(delivery: NotificationDelivery, error: str) -> None:
    now = timezone.now()
    if delivery.attempts >= _max_attempts():
        delivery.status = NotificationDelivery.STATUS_FAILED
        delivery.next_attempt_at = None
    else:
        delivery.next_attempt_at = now + _backoff(delivery.attempts)
    delivery.last_error = error[:2000]
    delivery.save(update_fields=["status", "next_attempt_at", "last_error"])
    if delivery.endpoint_id:
        WebhookEndpoint.objects.filter(pk=delivery.endpoint_id).update(
            last_delivery_at=now, last_status=f"error: {error}"[:40],
        )


# --- channels -------------------------------------------------------------------


def _send_email(delivery: NotificationDelivery) -> None:
    ev = events.get(delivery.event.event_key)
    target = delivery.event.target
    if target is None:
        raise RuntimeError("target no longer exists")
    body = render_to_string(
        "notifications/email/notification.txt",
        {
            "event": ev,
            "target": target,
            "actor": delivery.event.actor,
            "action_url": _action_url(ev, target),
        },
    )
    # from_email=None uses DEFAULT_FROM_EMAIL. fail_silently=False so a bad host raises into the
    # retry path rather than silently dropping the mail.
    send_mail(ev.subject, body, None, [delivery.email], fail_silently=False)


def _send_webhook(delivery: NotificationDelivery) -> None:
    endpoint = delivery.endpoint
    headers = {"Content-Type": "application/json", "User-Agent": "lumina-notifications"}
    if endpoint.kind == WebhookEndpoint.KIND_MATTERMOST:
        # A chat message to an incoming-webhook URL. No signature - the unguessable URL is the secret,
        # exactly as Slack/Mattermost expect.
        body = json.dumps(_mattermost_payload(delivery.event)).encode()
    else:
        body = json.dumps(_webhook_payload(delivery.event)).encode()
        signature = hmac.new(endpoint.secret.encode(), body, hashlib.sha256).hexdigest()
        headers |= {
            "X-Lumina-Event": delivery.event.event_key,
            "X-Lumina-Delivery": str(delivery.pk),
            "X-Lumina-Timestamp": str(int(delivery.event.created_at.timestamp())),
            "X-Lumina-Signature": f"sha256={signature}",
        }
    request = urllib.request.Request(endpoint.url, data=body, method="POST", headers=headers)
    with urllib.request.urlopen(request, timeout=_webhook_timeout()) as response:
        status = getattr(response, "status", 200)
        if not 200 <= status < 300:
            raise RuntimeError(f"endpoint returned HTTP {status}")


def _mattermost_payload(event: NotificationEvent) -> dict:
    """A Mattermost/Slack-style ``{"text": ...}`` message: the event, in markdown, with a link."""
    ev = events.get(event.event_key)
    subject = ev.subject if ev else event.event_key
    lines = [f"**{subject}**"]
    if ev and ev.description:
        lines.append(ev.description)
    url = _action_url(ev, event.target) if ev else _public_url(event.target)
    if url:
        lines.append(f"[Open in Lumina]({url})")
    return {"text": "\n".join(lines)}


def _webhook_payload(event: NotificationEvent) -> dict:
    target = event.target
    return {
        "event": event.event_key,
        "id": event.pk,
        "created_at": event.created_at.isoformat(),
        "target": {
            "type": event.target_content_type.model,
            "id": event.target_id,
            "url": _public_url(target),
        },
        "actor": event.actor.get_username() if event.actor else None,
    }


# --- audiences + links ----------------------------------------------------------


def _recipients(ev: events.Event, target) -> list[str]:
    if ev.audience == events.REVIEWERS:
        return _reviewer_emails()
    if ev.audience == events.SUBMITTER:
        return _owner_emails(target)
    if ev.audience == events.VENDOR_MEMBERS:
        return _vendor_member_emails(target)
    return []


def _reviewer_emails() -> list[str]:
    from django.contrib.auth import get_user_model

    from lumina.review.permissions import REVIEWER_GROUPS

    user_model = get_user_model()
    emails = set(
        user_model.objects.filter(groups__name__in=REVIEWER_GROUPS, is_active=True)
        .exclude(email="").values_list("email", flat=True)
    )
    emails.update(e for e in getattr(settings, "LUMINA_REVIEW_NOTIFY_EMAILS", []) if e)
    return sorted(emails)


def _owner_emails(target) -> list[str]:
    user = getattr(target, "submitter", None) or getattr(target, "requester", None)
    email = getattr(user, "email", "") if user is not None else ""
    return [email] if email else []


def _vendor_member_emails(target) -> list[str]:
    from lumina.vendors.models import VendorMembership

    vendor = (
        getattr(target, "owner_vendor", None)
        or getattr(target, "vendor", None)
        or getattr(getattr(target, "listing", None), "owner_vendor", None)
    )
    if vendor is None:
        return []
    return sorted(
        {
            m.user.email
            for m in vendor.memberships.filter(role__in=VendorMembership.SUBMIT_ROLES)
            .select_related("user")
            if m.user.email
        }
    )


def _public_url(target) -> str:
    getter = getattr(target, "get_absolute_url", None)
    return _base_url() + getter() if callable(getter) else _base_url()


def _action_url(ev: events.Event, target) -> str:
    if ev.audience == events.REVIEWERS:
        from django.urls import reverse

        return _base_url() + reverse("review:queue")
    return _public_url(target)


def _endpoints_for(event_key: str) -> list[WebhookEndpoint]:
    return [e for e in WebhookEndpoint.objects.filter(enabled=True) if e.wants(event_key)]
