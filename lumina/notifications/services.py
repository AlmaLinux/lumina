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

from lumina.notifications import events, payloads
from lumina.notifications.models import (
    NotificationDelivery,
    NotificationEndpoint,
    NotificationEvent,
    NotificationPolicy,
    PolicyDestination,
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
    """Turn one queued event into the deliveries its policy asks for.

    Every destination comes from the event's ``NotificationPolicy``. An event with no policy, or a
    disabled one, reaches nobody: the registry says what an event *is*, the policy says who is
    told, and there is no third rule hidden in this function.
    """
    ev = events.get(event.event_key)
    target = event.target
    # Unknown event key or a target deleted before delivery: nothing to send, but the event is still
    # marked processed by the caller so it stops being reconsidered.
    if ev is None or target is None:
        return
    policy = (
        NotificationPolicy.objects.filter(event_key=event.event_key, enabled=True)
        .prefetch_related("destinations__endpoint").first()
    )
    if policy is None:
        return
    now = timezone.now()
    for destination in policy.destinations.all():
        endpoint = destination.endpoint
        # ``webhookable`` gates anything that leaves for a third party; email is ours.
        if not destination.enabled or (
            not ev.webhookable and endpoint.kind != NotificationEndpoint.KIND_EMAIL
        ):
            continue
        if destination.kind == PolicyDestination.KIND_ROOM:
            _queue(event, now, destination.audience,
                   channel=NotificationDelivery.CHANNEL_WEBHOOK,
                   endpoint=endpoint, handle=destination.channel)
        elif endpoint.kind == NotificationEndpoint.KIND_EMAIL:
            for address in _emails_for(destination.audience, target):
                _queue(event, now, destination.audience,
                       channel=NotificationDelivery.CHANNEL_EMAIL,
                       endpoint=endpoint, email=address)
        else:
            for handle in _handles_for(destination.audience, target):
                _queue(event, now, destination.audience,
                       channel=NotificationDelivery.CHANNEL_CHAT,
                       endpoint=endpoint, handle=handle)


def _queue(event: NotificationEvent, now, audience: str, *, channel: str,
           email: str = "", endpoint=None, handle: str = "") -> None:
    """One delivery row, or nothing if it is already there.

    A post is one row whoever it is about; email and direct messages are one row per person, so a
    delivery fails and retries for the person it failed for rather than for the batch.
    """
    NotificationDelivery.objects.get_or_create(
        event=event, channel=channel, email=email, endpoint=endpoint, handle=handle,
        defaults={"next_attempt_at": now, "audience": audience},
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
        elif delivery.channel == NotificationDelivery.CHANNEL_CHAT:
            _send_chat(delivery)
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
        NotificationEndpoint.objects.filter(pk=delivery.endpoint_id).update(
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
        NotificationEndpoint.objects.filter(pk=delivery.endpoint_id).update(
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
            "action_url": _action_url(ev, target, delivery.audience),
        },
    )
    # from_email=None uses DEFAULT_FROM_EMAIL. fail_silently=False so a bad host raises into the
    # retry path rather than silently dropping the mail.
    send_mail(ev.subject, body, None, [delivery.email], fail_silently=False)


def _chat_payload(event, endpoint, audience: str = "") -> dict:
    """The body this endpoint's platform wants, with the target's own facts in it."""
    ev = events.get(event.event_key)
    url = _action_url(ev, event.target, audience) if ev else _public_url(event.target)
    builder = payloads.for_endpoint(endpoint.kind)
    return builder(event, url=url, fields=payloads.target_fields(event.target))


def _send_chat(delivery: NotificationDelivery) -> None:
    """Direct-message one person through a Mattermost incoming webhook.

    Mattermost routes an incoming webhook to a direct message when ``channel`` is
    ``@username``, which is why this needs no bot token and no API client: it is the same
    endpoint the channel notifications already use. It does require that the webhook is
    not locked to a single channel, which is what an admin says by giving the endpoint a
    ``chat_dm`` route.
    """
    endpoint = delivery.endpoint
    payload = _chat_payload(delivery.event, endpoint, delivery.audience) | {
        "channel": f"@{delivery.handle}"}
    body = json.dumps(payload).encode()
    request = urllib.request.Request(
        endpoint.url, data=body, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "lumina-notifications"},
    )
    with urllib.request.urlopen(request, timeout=_webhook_timeout()) as response:
        status = getattr(response, "status", 200)
        if not 200 <= status < 300:
            raise RuntimeError(f"endpoint returned HTTP {status}")


def _send_webhook(delivery: NotificationDelivery) -> None:
    endpoint = delivery.endpoint
    headers = {"Content-Type": "application/json", "User-Agent": "lumina-notifications"}
    # The body is the endpoint kind's business (see notifications.payloads); only whether
    # it is signed is decided here. Mattermost authenticates by the unguessable webhook
    # URL and has no shared secret to sign with, exactly as Slack and Mattermost expect;
    # the generic kind is consumed programmatically and is signed.
    payload = _chat_payload(delivery.event, endpoint, delivery.audience)
    # The route may name a channel, overriding the one the incoming webhook is locked to, which is
    # what lets one endpoint serve several rooms. Blank leaves the webhook's own channel alone.
    if delivery.handle and endpoint.is_chat:
        payload |= {"channel": delivery.handle}
    body = json.dumps(payload).encode()
    if endpoint.kind != NotificationEndpoint.KIND_MATTERMOST:
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


# --- audiences + links ----------------------------------------------------------


def _users_for(audience: str, target) -> list:
    """The people an audience resolves to, as users rather than addresses.

    One answer, because email and chat both need it and two rules that could disagree about who
    owns a submission is the sort of thing that mails the wrong person.
    """
    if audience == events.REVIEWERS:
        return _reviewer_users()
    if audience == events.SUBMITTER:
        return _owners(target)
    if audience == events.VENDOR_MEMBERS:
        return _vendor_members(target)
    return []


def _emails_for(audience: str, target) -> list[str]:
    """Email addresses for an audience, honouring each account's email preference.

    The preference defaults to on, so this is unchanged for anybody who has never opened their
    settings. Reviewers are subject to it too, which they were not before: once reviewer traffic
    has a channel to go to, a reviewer who has turned their email off has said something specific.
    Static ``LUMINA_REVIEW_NOTIFY_EMAILS`` entries are not accounts and have no preference to
    consult, so they always receive.
    """
    from lumina.accounts.models import NotificationDefaults

    defaults = NotificationDefaults.load()
    addresses = {
        user.email for user in _users_for(audience, target)
        if user.email and _wants_email(user, defaults)
    }
    if audience == events.REVIEWERS:
        addresses.update(e for e in getattr(settings, "LUMINA_REVIEW_NOTIFY_EMAILS", []) if e)
    return sorted(addresses)


def _handles_for(audience: str, target) -> list[str]:
    """Chat handles to direct-message for an audience.

    No audience is excluded here any more. Direct-messaging every reviewer individually is usually
    the same news three times, which is what a channel route is for, but that is now a decision an
    admin makes by not creating the route rather than one this function makes for them.
    """
    from lumina.accounts.models import AccountSettings, NotificationDefaults

    defaults = NotificationDefaults.load()
    return sorted({
        handle
        for handle in (
            AccountSettings.chat_handle_for(u, defaults) for u in _users_for(audience, target)
        )
        if handle
    })


def _wants_email(user, defaults=None) -> bool:
    """Whether this account takes email, following the site defaults when it has never said.

    ``defaults`` is passed in by callers asking about several people at once, so resolving an
    audience stays one query for the defaults rather than one per person.
    """
    from lumina.accounts.models import AccountSettings, NotificationDefaults

    row = AccountSettings.objects.filter(user=user).first()
    if row is not None:
        return row.email_notifications
    return (defaults or NotificationDefaults.load()).email_notifications


def _reviewer_users() -> list:
    from django.contrib.auth import get_user_model

    from lumina.review.permissions import REVIEWER_GROUPS

    return list(
        get_user_model().objects
        .filter(groups__name__in=REVIEWER_GROUPS, is_active=True).distinct()
    )


def _owners(target) -> list:
    """The one person an object belongs to, as a user rather than an address.

    Resolved as users because email and chat both need the same answer, and two audience
    rules that could disagree about who owns a submission is the sort of thing that mails
    the wrong person.
    """
    user = (
        getattr(target, "submitter", None)
        or getattr(target, "requester", None)
        # An edit proposal's owner. Named differently because it is a suggestion rather than a
        # submission, and the person who made it is the one owed the answer.
        or getattr(target, "proposed_by", None)
    )
    return [user] if user is not None else []


def _vendor_members(target) -> list:
    from lumina.vendors.models import VendorMembership

    vendor = (
        getattr(target, "owner_vendor", None)
        or getattr(target, "vendor", None)
        or getattr(getattr(target, "listing", None), "owner_vendor", None)
    )
    if vendor is None:
        return []
    return [
        m.user for m in vendor.memberships
        .filter(role__in=VendorMembership.SUBMIT_ROLES).select_related("user")
    ]


def _public_url(target) -> str:
    getter = getattr(target, "get_absolute_url", None)
    return _base_url() + getter() if callable(getter) else _base_url()


def _action_url(ev: events.Event, target, audience: str = "") -> str:
    """Where this message should send its reader.

    By the audience the route addressed, not by the event's default: one event can now reach a
    reviewer channel and its submitter at once, and they want different pages.
    """
    if (audience or ev.audience) == events.REVIEWERS:
        from django.urls import reverse

        from lumina.review.views import QUEUE_TABS

        url = _base_url() + reverse("review:queue")
        # The pane the event belongs to, validated against the panes that exist rather
        # than trusted: this string ends up in a mailed URL.
        if ev.tab and ev.tab in QUEUE_TABS:
            url = f"{url}?tab={ev.tab}"
        return url
    return _public_url(target)


