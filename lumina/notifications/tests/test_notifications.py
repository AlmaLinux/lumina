"""Notifications: events queued in-transaction, delivered out of band by the drainer.

Two properties matter most and are pinned here: enqueuing never does the slow work (so a submission
never blocks on SMTP/HTTP), and the drainer is idempotent and retry-safe. Plus the routing - which
audience hears about each event - and the webhook signature.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import urllib.error
from unittest import mock

import pytest
from django.contrib.auth.models import Group, User
from django.core import mail
from django.db import transaction
from django.test import override_settings
from django.utils import timezone

from lumina.notifications import services
from lumina.notifications.models import (
    NotificationDelivery,
    NotificationEvent,
    WebhookEndpoint,
)
from lumina.results import ingest
from lumina.results import services as run_services
from lumina.results.models import TestRun
from lumina.results.tests import factories as f

pytestmark = pytest.mark.django_db

WEBHOOK = "lumina.notifications.services.urllib.request.urlopen"


@pytest.fixture(autouse=True)
def _releases():
    from lumina.releases.models import AlmaLinuxRelease
    for major in (8, 9, 10):
        AlmaLinuxRelease.objects.get_or_create(major=major, defaults={"supported": True})


@pytest.fixture
def submitter():
    return User.objects.create_user("notif-sub", email="sub@example.com")


@pytest.fixture
def reviewer():
    user = User.objects.create_user("notif-rev", email="rev@example.com")
    group, _ = Group.objects.get_or_create(name="reviewer")
    user.groups.add(group)
    return user


def _draft_run(submitter) -> TestRun:
    """A validation run, which lands as a draft (and so ``ingest`` emits ``run.needs_details``)."""
    report = f.make_report(
        run_types=["validate"], results=[f.validate_result("validate.cpu.functional")],
    )
    return ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(f.build_bundle(report)), source="api",
    )


class _FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _reset():
    NotificationEvent.objects.all().delete()
    mail.outbox.clear()


def _recipients() -> set[str]:
    return {addr for message in mail.outbox for addr in message.to}


# --- enqueue is cheap and transactional -----------------------------------------


def test_ingesting_a_draft_enqueues_one_event(submitter):
    run = _draft_run(submitter)
    event = NotificationEvent.objects.get(event_key="run.needs_details")
    assert event.target == run and event.actor == submitter and event.processed_at is None


def test_an_event_rolls_back_with_its_transaction(submitter):
    run = _draft_run(submitter)
    before = NotificationEvent.objects.count()
    with pytest.raises(RuntimeError):
        with transaction.atomic():
            services.emit("run.submitted", target=run)
            raise RuntimeError("boom")
    assert NotificationEvent.objects.count() == before


@override_settings(LUMINA_NOTIFICATIONS_ENABLED=False)
def test_disabled_globally_enqueues_nothing(submitter):
    _draft_run(submitter)
    assert not NotificationEvent.objects.exists()


@override_settings(LUMINA_NOTIFY_DISABLED_EVENTS=["run.needs_details"])
def test_a_disabled_event_key_is_not_enqueued(submitter):
    _draft_run(submitter)
    assert not NotificationEvent.objects.filter(event_key="run.needs_details").exists()


def test_an_unknown_event_key_is_a_programming_error(submitter):
    run = _draft_run(submitter)
    with pytest.raises(ValueError, match="Unknown notification event"):
        services.emit("nope.nope", target=run)


# --- routing --------------------------------------------------------------------


def test_a_submitter_event_reaches_the_submitter(submitter):
    _draft_run(submitter)  # run.needs_details -> submitter
    services.deliver_pending()
    assert _recipients() == {"sub@example.com"}
    assert "https://" in mail.outbox[0].body  # an absolute link, built from LUMINA_SITE_BASE_URL


def test_a_reviewer_event_reaches_the_reviewers(submitter, reviewer):
    run = _draft_run(submitter)
    _reset()
    services.emit("run.submitted", target=run, actor=submitter)  # reviewers audience
    services.deliver_pending()
    assert _recipients() == {"rev@example.com"}


def test_request_changes_emails_the_submitter_out_of_band(submitter, reviewer):
    run = _draft_run(submitter)
    run.status = TestRun.STATUS_PENDING
    run.save(update_fields=["status"])
    _reset()

    run_services.request_run_changes(run, by=reviewer, reason="add the board model")
    # Nothing was sent in the request - only an outbox row.
    assert mail.outbox == []
    assert NotificationEvent.objects.filter(
        event_key="run.needs_changes", processed_at__isnull=True
    ).exists()

    services.deliver_pending()
    assert _recipients() == {"sub@example.com"}


# --- webhooks -------------------------------------------------------------------


def test_a_webhook_is_posted_and_signed(submitter):
    endpoint = WebhookEndpoint.objects.create(
        name="ops", url="https://hook.example/x", event_keys=["run.needs_details"],
    )
    assert endpoint.secret  # generated on save
    _draft_run(submitter)

    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["body"] = request.data
        captured["headers"] = {k.lower(): v for k, v in request.headers.items()}
        return _FakeResponse()

    with mock.patch(WEBHOOK, fake_urlopen):
        services.deliver_pending()

    assert captured["url"] == "https://hook.example/x"
    expected = "sha256=" + hmac.new(
        endpoint.secret.encode(), captured["body"], hashlib.sha256
    ).hexdigest()
    assert captured["headers"]["x-lumina-signature"] == expected
    assert json.loads(captured["body"])["event"] == "run.needs_details"
    delivery = NotificationDelivery.objects.get(channel=NotificationDelivery.CHANNEL_WEBHOOK)
    assert delivery.status == NotificationDelivery.STATUS_SENT


def test_a_mattermost_webhook_posts_a_chat_message_without_a_signature(submitter):
    WebhookEndpoint.objects.create(
        name="mm", url="https://mm.example/hooks/abc",
        kind=WebhookEndpoint.KIND_MATTERMOST, event_keys=["run.needs_details"],
    )
    _draft_run(submitter)

    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["headers"] = {k.lower(): v for k, v in request.headers.items()}
        captured["body"] = json.loads(request.data)
        return _FakeResponse()

    with mock.patch(WEBHOOK, fake_urlopen):
        services.deliver_pending()

    # Mattermost authenticates by the URL: no HMAC header, and a {"text": ...} chat message.
    assert "x-lumina-signature" not in captured["headers"]
    assert captured["body"]["text"] and "Open in Lumina" in captured["body"]["text"]
    delivery = NotificationDelivery.objects.get(channel=NotificationDelivery.CHANNEL_WEBHOOK)
    assert delivery.status == NotificationDelivery.STATUS_SENT


def test_a_mattermost_endpoint_needs_no_secret(submitter):
    endpoint = WebhookEndpoint.objects.create(
        name="mm", url="https://mm.example/hooks/abc", kind=WebhookEndpoint.KIND_MATTERMOST,
    )
    assert endpoint.secret == ""  # only the generic kind mints a signing secret


@override_settings(LUMINA_NOTIFY_MAX_ATTEMPTS=2)
def test_a_failing_webhook_retries_then_gives_up(submitter):
    WebhookEndpoint.objects.create(
        name="bad", url="https://bad.example/x", event_keys=["run.needs_details"],
    )
    _draft_run(submitter)

    def boom(request, timeout=None):
        raise urllib.error.URLError("connection refused")

    with mock.patch(WEBHOOK, boom):
        services.deliver_pending()
        delivery = NotificationDelivery.objects.get(channel=NotificationDelivery.CHANNEL_WEBHOOK)
        assert delivery.status == NotificationDelivery.STATUS_PENDING
        assert delivery.attempts == 1 and delivery.next_attempt_at is not None

        # Force it due and drain again: attempt 2 hits the cap and gives up.
        NotificationDelivery.objects.filter(pk=delivery.pk).update(next_attempt_at=timezone.now())
        services.deliver_pending()
        delivery.refresh_from_db()
        assert delivery.status == NotificationDelivery.STATUS_FAILED and delivery.attempts == 2


# --- idempotency ----------------------------------------------------------------


def test_draining_twice_neither_re_fans_nor_re_sends(submitter, reviewer):
    run = _draft_run(submitter)
    services.emit("run.submitted", target=run, actor=submitter)
    services.deliver_pending()

    deliveries = NotificationDelivery.objects.count()
    sent = len(mail.outbox)
    assert deliveries and sent

    services.deliver_pending()  # second pass
    assert NotificationDelivery.objects.count() == deliveries
    assert len(mail.outbox) == sent


# --- proposals ------------------------------------------------------------------


def test_a_listing_edit_proposal_notifies_reviewers(reviewer):
    from lumina.hardware.models import System
    from lumina.hardware.services import propose_listing_edit
    from lumina.vendors.models import Vendor, VendorMembership

    owner = User.objects.create_user("edit-proposer", email="owner@example.com")
    vendor = Vendor.objects.create(name="Acme")
    VendorMembership.objects.create(user=owner, vendor=vendor, role=VendorMembership.ROLE_OWNER)
    system = System.objects.create(
        vendor=vendor, owner_vendor=vendor, name="Server X", published=True,
    )

    propose_listing_edit(proposed_by=owner, listing=system, name="Server X (rev B)")

    assert NotificationEvent.objects.filter(event_key="proposal.created").exists()
    services.deliver_pending()
    assert "rev@example.com" in _recipients()


# --- admin config ---------------------------------------------------------------


def test_the_webhook_form_offers_registry_events_and_round_trips():
    from lumina.notifications import events
    from lumina.notifications.admin import WebhookEndpointForm

    # Choices come from the registry, not a freeform JSON box.
    assert {choice[0] for choice in WebhookEndpointForm().fields["event_keys"].choices} == set(
        events.EVENTS
    )

    # A ticked selection stores as a JSON list on the model and reloads pre-checked.
    form = WebhookEndpointForm(data={
        "name": "ops", "url": "https://hook.example/x", "kind": WebhookEndpoint.KIND_GENERIC,
        "event_keys": ["run.submitted", "submission.created"], "enabled": True,
    })
    assert form.is_valid(), form.errors
    endpoint = form.save()
    assert endpoint.event_keys == ["run.submitted", "submission.created"]
    assert set(WebhookEndpointForm(instance=endpoint).initial["event_keys"]) == {
        "run.submitted", "submission.created",
    }

    # An unknown key is rejected, not silently stored.
    assert not WebhookEndpointForm(data={
        "name": "x", "url": "https://h/x", "kind": WebhookEndpoint.KIND_GENERIC,
        "event_keys": ["nope.nope"],
    }).is_valid()
