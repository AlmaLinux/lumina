"""Direct-messaging the person a notice concerns, and letting them turn it off.

The platform already knows who somebody is, so a person should not have to retype their
name to be told their own submission was approved. The catch is that lumina usernames come
from Keycloak and a Mattermost account spelled the same way is not necessarily the same
human: sending on an assumed match would mail one person's submission status to a
stranger. So the handle defaults to the username and the person confirms it once, by
ticking the box.

Delivery reuses the Mattermost incoming webhook the channel notices already go through.
Mattermost routes such a POST to a direct message when ``channel`` is ``@username``, so
this needs no bot token and no API client - only an endpoint whose webhook is not locked
to one channel, which is what ``direct_messages`` records an admin having checked.
"""
from __future__ import annotations

import json
from unittest import mock

import pytest
from django.contrib.auth.models import Group, User
from django.core import mail

from lumina.accounts.models import AccountSettings
from lumina.notifications import services
from lumina.notifications.models import (
    NotificationDelivery,
    NotificationEvent,
    WebhookEndpoint,
)
from lumina.results.models import TestRun
from lumina.results.tests import factories as f

pytestmark = pytest.mark.django_db


class _FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def submitter():
    return User.objects.create_user("dm-sub", email="dmsub@example.com")


@pytest.fixture
def endpoint():
    return WebhookEndpoint.objects.create(
        name="AlmaLinux Mattermost", url="https://chat.invalid/hooks/abc",
        kind=WebhookEndpoint.KIND_MATTERMOST, direct_messages=True, enabled=True,
        event_keys=["run.needs_details", "run.approved"],
    )


def _draft_run(submitter) -> TestRun:
    from lumina.results import ingest

    return ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(f.build_bundle(f.make_report())),
        source="api",
    )


def _drain() -> list[dict]:
    posted = []

    def fake_urlopen(request, timeout=None):
        posted.append(json.loads(request.data.decode()))
        return _FakeResponse()

    with mock.patch("urllib.request.urlopen", fake_urlopen):
        services.deliver_pending()
    return posted


def _reset():
    NotificationEvent.objects.all().delete()
    NotificationDelivery.objects.all().delete()
    mail.outbox.clear()


# --- consent ---------------------------------------------------------------------

def test_nobody_is_messaged_until_they_ask(submitter, endpoint):
    """A Mattermost name that merely matches is not proof it is the same person, so an
    account that has said nothing gets no direct message."""
    _draft_run(submitter)

    posted = _drain()

    assert not any("channel" in payload for payload in posted)


def test_ticking_the_box_uses_the_username_without_retyping_it(submitter, endpoint):
    AccountSettings.objects.create(user=submitter, chat_dm=True)
    _draft_run(submitter)

    posted = _drain()

    dms = [p for p in posted if "channel" in p]
    assert [p["channel"] for p in dms] == ["@dm-sub"]
    assert "needs details" in dms[0]["text"].lower()


def test_a_different_mattermost_name_can_be_set(submitter, endpoint):
    AccountSettings.objects.create(
        user=submitter, chat_dm=True, chat_handle="alice.on.chat",
    )
    _draft_run(submitter)

    assert [p["channel"] for p in _drain() if "channel" in p] == ["@alice.on.chat"]


def test_the_at_sign_is_stripped_however_it_was_typed(submitter, endpoint):
    AccountSettings.objects.create(user=submitter, chat_dm=True, chat_handle="@alice")
    _draft_run(submitter)

    assert [p["channel"] for p in _drain() if "channel" in p] == ["@alice"]


# --- the two switches are independent --------------------------------------------

def test_email_can_be_turned_off_without_losing_chat(submitter, endpoint):
    AccountSettings.objects.create(
        user=submitter, chat_dm=True, email_notifications=False,
    )
    _draft_run(submitter)

    posted = _drain()

    assert mail.outbox == []
    assert [p["channel"] for p in posted if "channel" in p] == ["@dm-sub"]


def test_chat_can_be_turned_off_without_losing_email(submitter, endpoint):
    AccountSettings.objects.create(user=submitter, chat_dm=False)
    _draft_run(submitter)

    posted = _drain()

    assert [m.to for m in mail.outbox] == [["dmsub@example.com"]]
    assert not any("channel" in p for p in posted)


def test_an_account_that_never_opened_its_settings_still_gets_email(submitter, endpoint):
    _draft_run(submitter)

    _drain()

    assert [m.to for m in mail.outbox] == [["dmsub@example.com"]]


# --- what does and does not get direct-messaged ----------------------------------

def test_a_reviewer_event_is_not_direct_messaged(submitter, endpoint):
    """A shared queue wants the channel it already posts in; direct-messaging every
    reviewer individually would be the same news three times."""
    reviewer = User.objects.create_user("dm-rev", email="dmrev@example.com")
    reviewer.groups.add(Group.objects.get_or_create(name="reviewer")[0])
    AccountSettings.objects.create(user=reviewer, chat_dm=True)
    endpoint.event_keys = ["run.submitted"]
    endpoint.save(update_fields=["event_keys"])
    run = _draft_run(submitter)
    _reset()

    services.emit("run.submitted", target=run, actor=submitter)
    posted = _drain()

    assert not any("channel" in p for p in posted)
    assert posted, "the channel webhook still fired"


def test_an_endpoint_must_opt_in_to_direct_messages(submitter, endpoint):
    """A Mattermost webhook locked to one channel rejects a channel override, so an admin
    confirms this endpoint is not."""
    endpoint.direct_messages = False
    endpoint.save(update_fields=["direct_messages"])
    AccountSettings.objects.create(user=submitter, chat_dm=True)
    _draft_run(submitter)

    assert not any("channel" in p for p in _drain())


def test_an_unsubscribed_event_is_not_direct_messaged(submitter, endpoint):
    endpoint.event_keys = ["run.approved"]      # not run.needs_details
    endpoint.save(update_fields=["event_keys"])
    AccountSettings.objects.create(user=submitter, chat_dm=True)
    _draft_run(submitter)

    assert not any("channel" in p for p in _drain())


def test_a_failed_direct_message_retries_on_its_own(submitter, endpoint):
    AccountSettings.objects.create(user=submitter, chat_dm=True)
    _draft_run(submitter)

    def boom(request, timeout=None):
        raise OSError("chat is down")

    with mock.patch("urllib.request.urlopen", boom):
        services.deliver_pending()

    delivery = NotificationDelivery.objects.get(
        channel=NotificationDelivery.CHANNEL_CHAT
    )
    assert delivery.status == NotificationDelivery.STATUS_PENDING
    assert delivery.attempts == 1
    assert delivery.handle == "dm-sub"


def test_draining_twice_does_not_send_the_message_twice(submitter, endpoint):
    AccountSettings.objects.create(user=submitter, chat_dm=True)
    _draft_run(submitter)

    first = _drain()
    second = _drain()

    assert len([p for p in first if "channel" in p]) == 1
    assert second == []
