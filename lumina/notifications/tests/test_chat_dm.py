"""Direct-messaging the person a notice concerns, and letting them turn it off.

The platform already knows who somebody is, so a person does not retype their name to be told
their own submission was approved. The one thing they say is whether they want the messages at
all, which is the checkbox.

Delivery reuses the Mattermost incoming webhook the channel notices already go through.
Mattermost routes such a POST to a direct message when ``channel`` is ``@username``, so
this needs no bot token and no API client - only an endpoint whose webhook is not locked
to one channel, which is what an admin says by naming it as an event's direct-message endpoint.

The handle is the AlmaLinux username. It was configurable once, on the reasoning that a Mattermost
account spelled the same way need not be the same human; both platforms sign in through the same
account, so it cannot be anybody else.
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
    NotificationEndpoint,
    NotificationEvent,
    PolicyDestination,
)
from lumina.notifications.tests.helpers import policy_for
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
    """A Mattermost webhook that direct-messages the submitter about their own run.

    An endpoint is only a destination now; what reaches it is the routes' business, so the
    subscription that used to live on the endpoint is two rows here.
    """
    endpoint = NotificationEndpoint.objects.create(
        name="AlmaLinux Mattermost", url="https://chat.invalid/hooks/abc",
        kind=NotificationEndpoint.KIND_MATTERMOST, enabled=True,
    )
    for key in ("run.needs_details", "run.approved"):
        dm_route(endpoint, key)
    return endpoint


def dm_route(endpoint, event_key, audience="submitter"):
    return PolicyDestination.objects.create(
        policy=policy_for(event_key), endpoint=endpoint,
        kind=PolicyDestination.KIND_PERSON, audience=audience,
    )


def post_route(endpoint, event_key, audience="reviewers", channel=""):
    return PolicyDestination.objects.create(
        policy=policy_for(event_key), endpoint=endpoint, channel=channel, audience=audience,
        kind=PolicyDestination.KIND_ROOM,
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

def test_an_account_that_has_said_nothing_follows_the_site_default(submitter, endpoint):
    """Which is on. It was off, because a Mattermost name that merely matched was not proof of the
    same person; both platforms sign in through the same account, so it is."""
    from lumina.accounts.models import NotificationDefaults

    assert NotificationDefaults.load().chat_dm is True
    _draft_run(submitter)

    assert any("channel" in payload for payload in _drain())


def test_turning_the_default_off_silences_accounts_that_never_chose(submitter, endpoint):
    """The admin control, and the reason it is read per delivery rather than copied into each row
    at signup: it reaches the people who have never opened their settings, who are most people."""
    from lumina.accounts.models import NotificationDefaults

    defaults = NotificationDefaults.load()
    defaults.chat_dm = False
    defaults.save()
    _draft_run(submitter)

    assert not any("channel" in payload for payload in _drain())


def test_an_account_that_chose_is_not_overridden_by_the_default(submitter, endpoint):
    from lumina.accounts.models import NotificationDefaults

    AccountSettings.objects.create(user=submitter, chat_dm=False)
    defaults = NotificationDefaults.load()
    defaults.chat_dm = True
    defaults.save()
    _draft_run(submitter)

    assert not any("channel" in payload for payload in _drain())


def test_ticking_the_box_uses_the_username_without_retyping_it(submitter, endpoint):
    AccountSettings.objects.create(user=submitter, chat_dm=True)
    _draft_run(submitter)

    posted = _drain()

    dms = [p for p in posted if "channel" in p]
    assert [p["channel"] for p in dms] == ["@dm-sub"]
    assert "needs details" in dms[0]["text"].lower()


def test_the_handle_is_the_almalinux_username_and_nothing_else(submitter, endpoint):
    """There was a ``chat_handle`` field for a Mattermost name that differed. Both platforms sign
    in through the same account, so it cannot differ, and a field that can only ever be set wrong
    was a way to stop your own notifications arriving."""
    AccountSettings.objects.create(user=submitter, chat_dm=True)
    _draft_run(submitter)

    assert [p["channel"] for p in _drain() if "channel" in p] == [
        f"@{submitter.get_username()}"]


def test_a_username_with_an_at_sign_is_still_addressed_once(submitter, endpoint):
    """``effective_chat_handle`` used to strip an @ somebody had typed into the field. Nothing
    types it now, and the handle is built with exactly one."""
    AccountSettings.objects.create(user=submitter, chat_dm=True)
    _draft_run(submitter)

    channel = [p["channel"] for p in _drain() if "channel" in p][0]

    assert channel.startswith("@") and not channel.startswith("@@")


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

def test_a_reviewer_event_goes_to_the_room_not_to_each_reviewer(submitter, endpoint):
    """A shared queue wants the channel it already posts in; direct-messaging every reviewer
    individually would be the same news three times. Expressed as a post route rather than as a
    rule in the code, so an admin who does want the direct messages can ask for them."""
    reviewer = User.objects.create_user("dm-rev", email="dmrev@example.com")
    reviewer.groups.add(Group.objects.get_or_create(name="reviewer")[0])
    AccountSettings.objects.create(user=reviewer, chat_dm=True)
    post_route(endpoint, "run.submitted")
    run = _draft_run(submitter)
    _reset()

    services.emit("run.submitted", target=run, actor=submitter)
    posted = _drain()

    assert not any("@" in p.get("channel", "") for p in posted)
    assert posted, "the channel post still fired"


def test_an_endpoint_must_be_routed_for_direct_messages(submitter, endpoint):
    """A Mattermost webhook locked to one channel rejects a channel override, so an admin says
    which endpoint may direct-message by creating the route."""
    PolicyDestination.objects.filter(kind=PolicyDestination.KIND_PERSON).delete()
    AccountSettings.objects.create(user=submitter, chat_dm=True)
    _draft_run(submitter)

    assert not any("channel" in p for p in _drain())


def test_an_unrouted_event_is_not_direct_messaged(submitter, endpoint):
    PolicyDestination.objects.filter(
        kind=PolicyDestination.KIND_PERSON, policy__event_key="run.needs_details").delete()
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
