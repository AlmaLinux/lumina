"""A deployment can own a chat endpoint, so a rebuilt database comes back talking.

Endpoints and their routing are rows, and rows go when the database goes. The dev host is reset on
purpose and often, and on the morning after each one nobody notices that chat has stopped until
somebody asks why the queue is quiet. So the webhook URL comes from the environment (a GitHub
secret on dev, vault in production) and a deploy reconciles the rows to match.

These pin the two halves of that: what a reconcile creates, and what it refuses to touch. The
second is the half that matters on a live system, where an admin has opinions of their own.
"""
from __future__ import annotations

import dataclasses
import json
from io import StringIO
from unittest import mock

import pytest
from django.contrib.auth.models import Group, User
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from lumina.notifications import events, provisioning, services
from lumina.notifications.models import (
    NotificationEndpoint,
    PolicyDestination,
)
from lumina.results.tests import factories as f

pytestmark = pytest.mark.django_db

URL = "https://chat.invalid/hooks/aaaaaaaaaaaaaaaaaaaaaaaaaa"
OTHER_URL = "https://chat.invalid/hooks/bbbbbbbbbbbbbbbbbbbbbbbbbb"


def configured(url: str = URL, channel: str = "") -> provisioning.Outcome:
    return provisioning.configure_mattermost(url=url, channel=channel)


def rooms(endpoint: NotificationEndpoint):
    """Which events post to this endpoint, and where."""
    return {
        destination.policy.event_key: destination.channel
        for destination in PolicyDestination.objects.filter(
            endpoint=endpoint, kind=PolicyDestination.KIND_ROOM,
        ).select_related("policy")
    }


# --- what a fresh database gets -------------------------------------------------


def test_a_wiped_database_comes_back_with_the_endpoint_the_deployment_configured():
    NotificationEndpoint.objects.all().delete()

    outcome = configured()

    endpoint = NotificationEndpoint.objects.get(kind=NotificationEndpoint.KIND_MATTERMOST)
    assert (outcome.created, outcome.endpoint) == (True, endpoint)
    assert endpoint.url == URL
    assert endpoint.enabled


def test_every_event_a_reviewer_has_to_act_on_posts_there():
    """Read off the registry, so an event added in a release is routed by the next deploy rather
    than being quietly unannounced."""
    outcome = configured()

    expected = {key for key, event in events.EVENTS.items() if event.audience == events.REVIEWERS}
    assert expected, "the registry has reviewer events; a change that empties it breaks this"
    assert set(rooms(outcome.endpoint)) == expected


def test_a_personal_outcome_is_not_posted_into_a_shared_room():
    """The rule the channel exists under. "Your run was rejected" is for its submitter, and a room
    full of reviewers is not that person."""
    outcome = configured()

    personal = {key for key, event in events.EVENTS.items() if event.audience == events.SUBMITTER}
    assert personal & set(rooms(outcome.endpoint)) == set()


def test_the_room_is_addressed_as_reviewers_so_its_links_open_the_queue():
    """The audience on a post decides where the link points, not only who hears. A reviewer
    message that opens the object rather than the queue makes the reader find it again."""
    outcome = configured()

    audiences = set(PolicyDestination.objects.filter(
        endpoint=outcome.endpoint).values_list("audience", flat=True))
    assert audiences == {events.REVIEWERS}


def test_an_event_that_never_leaves_the_building_is_not_routed(monkeypatch):
    """``webhookable`` is honoured by fan-out, so a destination for one of those would be a row
    that can only ever be skipped."""
    internal = dataclasses.replace(
        events.EVENTS["run.submitted"], key="internal.only", webhookable=False)
    monkeypatch.setitem(events.EVENTS, "internal.only", internal)

    assert "internal.only" not in rooms(configured().endpoint)


def test_an_event_added_in_a_release_is_routed_by_the_next_deploy(monkeypatch):
    added = dataclasses.replace(events.EVENTS["run.submitted"], key="run.escalated")
    configured()
    monkeypatch.setitem(events.EVENTS, "run.escalated", added)

    outcome = configured()

    assert outcome.routed == ["run.escalated"]
    assert "run.escalated" in rooms(outcome.endpoint)


# --- running it again, which is every deploy ------------------------------------


def test_a_second_run_changes_nothing():
    """It runs on every deploy. Anything not idempotent here is a channel posting twice."""
    first = configured()
    before = set(PolicyDestination.objects.values_list("pk", flat=True))

    second = configured()

    assert (second.created, second.routed, second.rechanneled) == (False, [], [])
    assert second.endpoint == first.endpoint
    assert set(PolicyDestination.objects.values_list("pk", flat=True)) == before
    assert NotificationEndpoint.objects.filter(
        kind=NotificationEndpoint.KIND_MATTERMOST).count() == 1


def test_a_rotated_url_updates_the_endpoint_rather_than_adding_a_second():
    """Two endpoints with the same routing is not a stale endpoint, it is every message twice."""
    configured()

    outcome = configured(url=OTHER_URL)

    assert outcome.url_changed and not outcome.created
    assert NotificationEndpoint.objects.get(
        kind=NotificationEndpoint.KIND_MATTERMOST).url == OTHER_URL


def test_a_changed_channel_moves_the_posts_instead_of_duplicating_them():
    """The channel is part of what makes a destination unique, so creating one unconditionally
    would leave the old row in place and post everything to both rooms."""
    endpoint = configured(channel="lumina-reviews").endpoint

    outcome = configured(channel="hardware-cert")

    assert outcome.rechanneled and not outcome.routed
    assert set(rooms(endpoint).values()) == {"hardware-cert"}


def test_a_channel_written_with_a_hash_is_the_same_channel():
    """Mattermost wants the name as it appears in the URL bar; "#name" is not that name and the
    post is rejected. Somebody will write the hash."""
    endpoint = configured(channel="lumina-reviews").endpoint

    outcome = configured(channel="#lumina-reviews")

    assert outcome.rechanneled == []
    assert set(rooms(endpoint).values()) == {"lumina-reviews"}


# --- what it refuses to touch ---------------------------------------------------


def test_no_url_configures_nothing_and_leaves_the_admin_alone():
    """Blank is how an installation says it has no chat, not how it says "delete what I set up"."""
    mine = NotificationEndpoint.objects.create(
        name="somebody's room", url=URL, kind=NotificationEndpoint.KIND_MATTERMOST)

    outcome = configured(url="   ")

    assert (outcome.endpoint, outcome.created, outcome.routed) == (None, False, [])
    assert NotificationEndpoint.objects.filter(pk=mine.pk).exists()


def test_an_endpoint_switched_off_in_the_admin_stays_off():
    """Disabling a noisy endpoint is a decision somebody made about a live system, and a deploy
    is not an argument against it."""
    endpoint = configured().endpoint
    NotificationEndpoint.objects.filter(pk=endpoint.pk).update(enabled=False)

    configured(url=OTHER_URL)

    endpoint.refresh_from_db()
    assert not endpoint.enabled
    assert endpoint.url == OTHER_URL, "the URL is still kept in step"


def test_a_room_switched_off_in_the_admin_stays_off():
    endpoint = configured().endpoint
    post = PolicyDestination.objects.filter(endpoint=endpoint).first()
    PolicyDestination.objects.filter(pk=post.pk).update(enabled=False)

    configured()

    post.refresh_from_db()
    assert not post.enabled


def test_an_endpoint_somebody_else_added_is_not_reconciled():
    """One deployment-owned endpoint, found by its name. Everything else on the system belongs to
    whoever created it, including another Mattermost workspace."""
    theirs = NotificationEndpoint.objects.create(
        name="release team", url=OTHER_URL, kind=NotificationEndpoint.KIND_MATTERMOST)

    configured()

    theirs.refresh_from_db()
    assert theirs.url == OTHER_URL
    assert rooms(theirs) == {}


def test_an_endpoint_of_another_kind_with_the_same_name_is_not_taken_over():
    """The name alone is not the key. A generic endpoint posts a signed JSON envelope to something
    parsing it, and quietly turning that into a chat room sends every consumer a payload it has no
    idea what to do with."""
    theirs = NotificationEndpoint.objects.create(
        name=provisioning.MATTERMOST_ENDPOINT_NAME, url=OTHER_URL,
        kind=NotificationEndpoint.KIND_GENERIC)

    outcome = configured()

    theirs.refresh_from_db()
    assert (theirs.url, rooms(theirs)) == (OTHER_URL, {})
    assert outcome.created and outcome.endpoint != theirs


def test_the_email_routing_the_migration_seeds_is_untouched():
    """Chat is added beside mail, not instead of it: somebody who does not sit in the channel
    still gets told."""
    before = set(PolicyDestination.objects.filter(
        endpoint__kind=NotificationEndpoint.KIND_EMAIL).values_list("pk", flat=True))

    configured()

    assert set(PolicyDestination.objects.filter(
        endpoint__kind=NotificationEndpoint.KIND_EMAIL).values_list("pk", flat=True)) == before


def test_a_url_that_is_not_one_is_refused():
    """Stored instead, a typo is invisible until every delivery has exhausted its five attempts
    against an address nobody reads."""
    with pytest.raises(ValidationError):
        configured(url="chat.invalid/hooks/no-scheme")

    assert not NotificationEndpoint.objects.filter(
        kind=NotificationEndpoint.KIND_MATTERMOST).exists()


def test_a_scheme_no_webhook_consumer_speaks_is_refused():
    with pytest.raises(ValidationError):
        configured(url="ftp://chat.invalid/hooks/x")


# --- and then it actually posts -------------------------------------------------


class _FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def reviewer():
    user = User.objects.create_user("prov-rev", email="provrev@example.com")
    user.groups.add(Group.objects.get_or_create(name="reviewer")[0])
    return user


def test_a_reviewer_event_reaches_the_configured_channel(reviewer):
    """End to end, because every part of this is only worth having if a message arrives: the
    endpoint, the route, the channel override, and the URL the POST goes to."""
    submitter = User.objects.create_user("prov-sub", email="provsub@example.com")
    from lumina.results import ingest

    run = ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(f.build_bundle(f.make_report())),
        source="api",
    )
    configured(channel="lumina-reviews")
    services.emit("run.submitted", target=run)

    posted = []

    def fake_urlopen(request, timeout=None):
        posted.append((request.full_url, json.loads(request.data.decode())))
        return _FakeResponse()

    with mock.patch("urllib.request.urlopen", fake_urlopen):
        services.deliver_pending()

    assert [url for url, _ in posted] == [URL]
    body = posted[0][1]
    assert body["channel"] == "lumina-reviews"
    assert events.EVENTS["run.submitted"].subject in body["text"]


# --- the command the deploy runs ------------------------------------------------


def run_command() -> str:
    out = StringIO()
    call_command("configure_notifications", stdout=out)
    return out.getvalue()


@override_settings(LUMINA_MATTERMOST_WEBHOOK_URL=URL, LUMINA_MATTERMOST_CHANNEL="lumina-reviews")
def test_the_command_configures_what_the_environment_names():
    output = run_command()

    endpoint = NotificationEndpoint.objects.get(kind=NotificationEndpoint.KIND_MATTERMOST)
    assert set(rooms(endpoint).values()) == {"lumina-reviews"}
    assert "Created" in output


@override_settings(LUMINA_MATTERMOST_WEBHOOK_URL=URL, LUMINA_MATTERMOST_CHANNEL="")
def test_the_command_never_prints_the_url():
    """It runs where its output is a CI log, and a Mattermost incoming-webhook URL is the
    credential: anybody holding it can post into the channel."""
    assert URL not in run_command()


@override_settings(LUMINA_MATTERMOST_WEBHOOK_URL="", LUMINA_MATTERMOST_CHANNEL="")
def test_the_command_is_a_no_op_with_nothing_configured():
    """Every deploy runs it, including the ones for installations that have no chat at all."""
    output = run_command()

    assert not NotificationEndpoint.objects.filter(
        kind=NotificationEndpoint.KIND_MATTERMOST).exists()
    assert "not set" in output


@override_settings(LUMINA_MATTERMOST_WEBHOOK_URL="not-a-url")
def test_the_command_fails_the_deploy_on_a_url_it_cannot_use():
    with pytest.raises(CommandError, match="not a usable URL"):
        run_command()


@override_settings(LUMINA_MATTERMOST_WEBHOOK_URL=URL)
def test_the_command_says_when_the_endpoint_is_disabled():
    """Otherwise a deploy that looks entirely successful leaves chat silent, and the reason is a
    checkbox somebody cleared weeks ago."""
    run_command()
    NotificationEndpoint.objects.filter(
        kind=NotificationEndpoint.KIND_MATTERMOST).update(enabled=False)

    assert "disabled in the admin" in run_command()


# --- and the admin says who owns what -------------------------------------------


def configured_by(endpoint) -> str:
    from lumina.notifications.admin import NotificationEndpointAdmin

    return NotificationEndpointAdmin.configured_by(None, endpoint)


@override_settings(LUMINA_MATTERMOST_WEBHOOK_URL=URL)
def test_the_admin_says_which_endpoint_the_deployment_will_rewrite():
    """Otherwise the way to find out is to edit the URL and watch it change back after a deploy."""
    endpoint = configured().endpoint
    theirs = NotificationEndpoint.objects.create(
        name="release team", url=OTHER_URL, kind=NotificationEndpoint.KIND_MATTERMOST)
    # Same name, different platform: the reconcile passes it over, so nothing here is at risk.
    namesake = NotificationEndpoint.objects.create(
        name=provisioning.MATTERMOST_ENDPOINT_NAME, url=OTHER_URL,
        kind=NotificationEndpoint.KIND_GENERIC)

    assert "the deployment" in configured_by(endpoint)
    assert configured_by(theirs) == "this page"
    assert configured_by(namesake) == "this page"


@override_settings(LUMINA_MATTERMOST_WEBHOOK_URL="")
def test_nothing_is_owned_by_a_deployment_that_configures_nothing():
    """An installation that has dropped the variable has stopped owning the row it once made, and
    saying otherwise would warn about an overwrite that cannot happen."""
    endpoint = NotificationEndpoint.objects.create(
        name=provisioning.MATTERMOST_ENDPOINT_NAME, url=URL,
        kind=NotificationEndpoint.KIND_MATTERMOST)

    assert configured_by(endpoint) == "this page"
