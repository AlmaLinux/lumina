"""Who hears about an event, and how, is a table an admin owns.

Routing used to be split between a hardcoded audience on the event and a boolean on the endpoint,
which made four things wrong at once: a channel post ignored the audience and published personal
outcomes into a shared room, an endpoint that could direct-message also posted the same news to its
channel, reviewer email had no switch but an environment variable, and nothing could say which
channel a post lands in. These pin each of those, and the rule that replaced them: an event reaches
exactly the destinations its policy names, and no others.
"""
from __future__ import annotations

import dataclasses
import json
from unittest import mock

import pytest
from django.contrib.auth.models import Group, User
from django.core import mail
from django.core.exceptions import ValidationError

from lumina.accounts.models import AccountSettings
from lumina.notifications import services
from lumina.notifications.models import (
    NotificationDelivery,
    NotificationEndpoint,
    NotificationEvent,
    NotificationPolicy,
    PolicyDestination,
)
from lumina.notifications.tests.helpers import email_endpoint, email_to, policy_for
from lumina.results.tests import factories as f

pytestmark = pytest.mark.django_db

WEBHOOK = "urllib.request.urlopen"


class _FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def submitter():
    return User.objects.create_user("route-sub", email="routesub@example.com")


@pytest.fixture
def reviewer():
    user = User.objects.create_user("route-rev", email="routerev@example.com")
    user.groups.add(Group.objects.get_or_create(name="reviewer")[0])
    return user


@pytest.fixture
def room():
    """A Mattermost webhook that posts into a channel."""
    return NotificationEndpoint.objects.create(
        name="reviewers room", url="https://chat.invalid/hooks/room",
        kind=NotificationEndpoint.KIND_MATTERMOST, enabled=True,
    )


def post_to(event_key, audience, endpoint, channel="", enabled=True):
    return PolicyDestination.objects.create(
        policy=policy_for(event_key), endpoint=endpoint, channel=channel,
        audience=audience, enabled=enabled, kind=PolicyDestination.KIND_ROOM,
    )


def dm_to(event_key, audience, endpoint):
    return PolicyDestination.objects.create(
        policy=policy_for(event_key), endpoint=endpoint,
        kind=PolicyDestination.KIND_PERSON, audience=audience,
    )


def only(event_key, *, email_audiences=()):
    """This event's policy, with every other destination on the system cleared."""
    PolicyDestination.objects.exclude(policy__event_key=event_key).delete()
    return email_to(event_key, *email_audiences)


def _draft_run(submitter):
    from lumina.results import ingest

    return ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(f.build_bundle(f.make_report())),
        source="api",
    )


def _reset():
    NotificationDelivery.objects.all().delete()
    NotificationEvent.objects.all().delete()
    mail.outbox.clear()


def _posts() -> list[dict]:
    """Drain, capturing every body that would have been POSTed."""
    sent = []

    def fake_urlopen(request, timeout=None):
        sent.append(json.loads(request.data.decode()))
        return _FakeResponse()

    with mock.patch(WEBHOOK, fake_urlopen):
        services.deliver_pending()
    return sent


# --- an event goes where its routes say, and nowhere else -----------------------


def test_an_event_with_no_policy_reaches_nobody(submitter):
    """The off switch. Turning an event off used to need an environment variable and a deploy."""
    NotificationPolicy.objects.all().delete()

    _draft_run(submitter)

    assert _posts() == []
    assert mail.outbox == []


def test_a_room_is_not_told_about_somebody_elses_personal_outcome(submitter, room):
    """A post used to go to every endpoint subscribed to the key, whatever the event was about, so
    a shared channel published private outcomes. A post now happens because a route asked for it."""
    post_to("run.submitted", "reviewers", room)

    _draft_run(submitter)  # raises run.needs_details, a submitter event

    assert _posts() == []


def test_a_room_is_told_about_the_queue(submitter, reviewer, room):
    post_to("run.needs_details", "reviewers", room)

    _draft_run(submitter)

    assert len(_posts()) == 1


def test_an_endpoint_that_direct_messages_does_not_also_post_the_same_news(submitter, room):
    """One ``event_keys`` list drove both the post and the direct message, so an endpoint
    configured for both sent everything twice. Two transports are two rows now."""
    AccountSettings.objects.create(user=submitter, chat_dm=True)
    dm_to("run.needs_details", "submitter", room)
    _draft_run(submitter)

    posts = _posts()

    assert len(posts) == 1
    assert posts[0]["channel"] == f"@{submitter.username}"


def test_only_the_routed_endpoint_is_posted_to(submitter, room):
    """Two rooms, one route. A post reaches the endpoint a route names, not every endpoint that
    happens to be enabled."""
    other = NotificationEndpoint.objects.create(
        name="unrelated", url="https://chat.invalid/hooks/other",
        kind=NotificationEndpoint.KIND_MATTERMOST, enabled=True,
    )
    post_to("run.needs_details", "reviewers", room)
    _draft_run(submitter)

    assert len(_posts()) == 1
    assert not NotificationDelivery.objects.filter(endpoint=other).exists()


def test_a_disabled_policy_delivers_nothing(submitter, room):
    """Turning the whole event off, as opposed to one of its destinations."""
    post_to("run.needs_details", "reviewers", room)
    policy_for("run.needs_details", enabled=False)
    _draft_run(submitter)

    assert _posts() == []
    assert mail.outbox == []


def test_a_disabled_direct_message_is_not_attempted(submitter, room):
    """Turning off one destination of several, which is what the list is for."""
    PolicyDestination.objects.create(
        policy=policy_for("run.needs_details"), endpoint=room,
        kind=PolicyDestination.KIND_PERSON, audience="submitter", enabled=False,
    )
    AccountSettings.objects.create(user=submitter, chat_dm=True)
    _draft_run(submitter)

    assert _posts() == []
    assert not NotificationDelivery.objects.filter(
        channel=NotificationDelivery.CHANNEL_CHAT).exists()


def test_a_disabled_post_delivers_nothing(submitter, room):
    """The switch an admin actually reaches for: leave the rule in place, turn it off."""
    post_to("run.needs_details", "reviewers", room, enabled=False)
    _draft_run(submitter)

    assert _posts() == []


# --- which room ------------------------------------------------------------------


def test_a_route_can_name_the_channel_to_post_in(submitter, room):
    """One incoming webhook can serve several rooms, which was impossible while the channel was
    whatever the webhook URL was locked to."""
    post_to("run.needs_details", "reviewers", room, channel="hardware-review")
    _draft_run(submitter)

    assert _posts()[0]["channel"] == "hardware-review"


def test_a_route_with_no_channel_leaves_the_webhooks_own_alone(submitter, room):
    post_to("run.needs_details", "reviewers", room)
    _draft_run(submitter)

    assert "channel" not in _posts()[0]


def test_a_generic_endpoint_is_never_given_a_channel(submitter):
    """A programmatic consumer has no rooms, and a stray key in its JSON is a field it did not
    ask for. The form refuses the combination; this is the delivery side of the same rule."""
    endpoint = NotificationEndpoint.objects.create(
        name="ops", url="https://hook.invalid/x", kind=NotificationEndpoint.KIND_GENERIC,
    )
    post_to("run.needs_details", "reviewers", endpoint, channel="ignored")
    _draft_run(submitter)

    assert "channel" not in _posts()[0]


# --- who, and where the link points ---------------------------------------------


def test_the_link_follows_the_route_not_the_events_default(submitter, reviewer, room):
    """One event can now reach a reviewer channel and its submitter at once, and they want
    different pages: the queue for one, the object for the other."""
    post_to("run.needs_details", "reviewers", room)
    AccountSettings.objects.create(user=submitter, chat_dm=True)
    dm_to("run.needs_details", "submitter", room)
    _draft_run(submitter)

    links = {post["attachments"][0]["title_link"] for post in _posts()}

    assert len(links) == 2
    assert any("/review/" in link for link in links)
    assert any("/review/" not in link for link in links)


def test_turning_the_email_default_off_silences_accounts_that_never_chose(submitter, reviewer):
    """The admin control on the email side. An account with no settings row follows the site
    default, which is how a change reaches the people who have never opened their settings."""
    from lumina.accounts.models import NotificationDefaults

    defaults = NotificationDefaults.load()
    defaults.email_notifications = False
    defaults.save()
    only("run.needs_details", email_audiences=("submitter",))

    _draft_run(submitter)
    services.deliver_pending()

    assert mail.outbox == []


def test_a_reviewer_who_turned_email_off_is_not_mailed(submitter, reviewer):
    """Reviewers were exempt from the preference, because there was nothing else for them to
    receive. Now that there is a channel, a reviewer who turned email off has said something."""
    AccountSettings.objects.create(user=reviewer, email_notifications=False)
    only("run.needs_details", email_audiences=("reviewers",))

    _draft_run(submitter)
    services.deliver_pending()

    assert mail.outbox == []


def test_the_static_reviewer_list_is_not_an_account_and_still_receives(submitter, settings):
    settings.LUMINA_REVIEW_NOTIFY_EMAILS = ["ops@example.com"]
    only("run.needs_details", email_audiences=("reviewers",))

    _draft_run(submitter)
    services.deliver_pending()

    assert [m.to for m in mail.outbox] == [["ops@example.com"]]


def test_one_person_who_is_both_audiences_gets_one_message(submitter):
    """The submitter of their own run who also reviews. Two routes, one mail: the delivery is
    keyed on the address, and the audience rides along rather than splitting it."""
    submitter.groups.add(Group.objects.get_or_create(name="reviewer")[0])
    only("run.needs_details", email_audiences=("reviewers", "submitter"))

    _draft_run(submitter)
    services.deliver_pending()

    assert len(mail.outbox) == 1


# --- a policy that cannot work is refused when it is typed ------------------------


def test_email_has_no_rooms_to_post_to():
    """Treated as direct only, deliberately: a group alias is one, and calling it a room would put
    personal outcomes in a shared mailbox."""
    with pytest.raises(ValidationError):
        PolicyDestination(
            policy=policy_for("run.approved"), endpoint=email_endpoint(),
            kind=PolicyDestination.KIND_ROOM,
        ).clean()


def test_a_generic_consumer_has_no_people_to_address():
    generic = NotificationEndpoint.objects.create(
        name="ops", url="https://hook.invalid/x", kind=NotificationEndpoint.KIND_GENERIC,
    )
    with pytest.raises(ValidationError):
        PolicyDestination(
            policy=policy_for("run.approved"), endpoint=generic,
            kind=PolicyDestination.KIND_PERSON,
        ).clean()


def test_direct_messages_need_a_chat_endpoint():
    generic = NotificationEndpoint.objects.create(
        name="ops", url="https://hook.invalid/x", kind=NotificationEndpoint.KIND_GENERIC,
    )
    with pytest.raises(ValidationError):
        PolicyDestination(
            policy=policy_for("run.approved"), endpoint=generic,
            kind=PolicyDestination.KIND_PERSON, audience="submitter",
        ).clean()


def test_a_direct_message_goes_to_a_person_not_a_room(room):
    with pytest.raises(ValidationError):
        PolicyDestination(
            policy=policy_for("run.approved"), endpoint=room,
            kind=PolicyDestination.KIND_PERSON, channel="general",
        ).clean()


def test_a_generic_endpoint_has_no_channel_to_choose(room):
    generic = NotificationEndpoint.objects.create(
        name="ops", url="https://hook.invalid/x", kind=NotificationEndpoint.KIND_GENERIC,
    )
    with pytest.raises(ValidationError):
        PolicyDestination(
            policy=policy_for("run.approved"), endpoint=generic, channel="general",
        ).clean()


def test_a_workable_policy_passes(room):
    for destination in (
        PolicyDestination(policy=policy_for("run.approved"), endpoint=email_endpoint(),
                          kind=PolicyDestination.KIND_PERSON, audience="submitter"),
        PolicyDestination(policy=policy_for("run.approved"), endpoint=room,
                          kind=PolicyDestination.KIND_ROOM, channel="general"),
        PolicyDestination(policy=policy_for("run.approved"), endpoint=room,
                          kind=PolicyDestination.KIND_PERSON, audience="submitter"),
    ):
        destination.clean()


# --- the seeded table --------------------------------------------------------------


def test_an_event_that_may_not_leave_the_building_is_still_emailed(submitter, room):
    """``webhookable`` gates what goes to a third party. Email is ours, and it stayed exempt when
    it became an endpoint: an event marked not-webhookable must still reach its own people."""
    from lumina.notifications import events

    post_to("run.needs_details", "reviewers", room)
    monkey = events.EVENTS["run.needs_details"]
    events.EVENTS["run.needs_details"] = dataclasses.replace(monkey, webhookable=False)
    try:
        _draft_run(submitter)
        posts = _posts()
    finally:
        events.EVENTS["run.needs_details"] = monkey

    assert posts == [], "a non-webhookable event left for a third party"
    assert mail.outbox, "and its own people were not told"


def test_email_is_an_endpoint_like_any_other():
    """It used to be a field on the policy, which made the built-in transport the one thing shaped
    differently from everything else."""
    assert NotificationEndpoint.objects.filter(
        kind=NotificationEndpoint.KIND_EMAIL).exists(), "the migration seeds one"
    assert PolicyDestination.objects.filter(
        endpoint__kind=NotificationEndpoint.KIND_EMAIL).exists()


def test_every_registered_event_has_a_policy():
    """An event with no policy is silently dead, so this is the guard for one being added to the
    registry and never configured."""
    from lumina.notifications.events import EVENTS

    configured = set(NotificationPolicy.objects.values_list("event_key", flat=True))

    assert set(EVENTS) - configured == set()


# --- more than one platform, and both at once ------------------------------------
#
# The shape this is heading for: Slack beside Mattermost rather than several Mattermost instances.
# The old policy held one DM endpoint and a separate list of rooms, which could not express it.


def test_one_event_can_direct_message_on_two_platforms(submitter, room):
    """A single ``dm_endpoint`` made this impossible to say. Two rows say it."""
    second = NotificationEndpoint.objects.create(
        name="other workspace", url="https://chat2.invalid/hooks/abc",
        kind=NotificationEndpoint.KIND_MATTERMOST, enabled=True,
    )
    AccountSettings.objects.create(user=submitter, chat_dm=True)
    dm_to("run.needs_details", "submitter", room)
    PolicyDestination.objects.create(
        policy=policy_for("run.needs_details"), endpoint=second,
        kind=PolicyDestination.KIND_PERSON, audience="submitter",
    )
    _draft_run(submitter)

    _posts()
    endpoints = set(
        NotificationDelivery.objects
        .filter(channel=NotificationDelivery.CHANNEL_CHAT)
        .values_list("endpoint__name", flat=True)
    )

    assert endpoints == {"reviewers room", "other workspace"}


def test_one_event_can_reach_a_room_and_a_person_through_the_same_endpoint(submitter, room):
    """The two used to be different mechanisms; they are two rows of one list now."""
    AccountSettings.objects.create(user=submitter, chat_dm=True)
    post_to("run.needs_details", "reviewers", room, channel="hardware-review")
    dm_to("run.needs_details", "submitter", room)
    _draft_run(submitter)

    channels = [post.get("channel") for post in _posts()]

    assert sorted(channels) == ["@route-sub", "hardware-review"]


def test_the_endpoint_says_whether_its_platform_has_people_to_address(room):
    """The one place a new chat platform is named. A generic webhook has one consumer and no
    rooms; a chat endpoint has both."""
    generic = NotificationEndpoint.objects.create(
        name="ops", url="https://hook.invalid/x", kind=NotificationEndpoint.KIND_GENERIC,
    )

    assert room.is_chat is True
    assert generic.is_chat is False
