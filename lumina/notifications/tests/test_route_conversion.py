"""The routes an admin already configured survive the move to policies.

Routes shipped and ran before the grain was reconsidered, so this conversion runs against real
configuration rather than an empty table: getting it wrong loses somebody's configuration silently, and
a notification that stops arriving is not a failure anybody sees. Driven through Django's own
migration executor so it is the migration being tested, not a copy of its logic.
"""
from __future__ import annotations

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader

pytestmark = pytest.mark.django_db(transaction=True)

BEFORE = [("notifications", "0008_notificationpolicy_policypost")]
# The head of the app, looked up rather than named: the conversion has migrations after it now,
# and a fixture that put the database back to a middle state left every later test on the wrong
# schema.
AFTER = MigrationLoader(None, ignore_no_migrations=True).graph.leaf_nodes("notifications")


def _migrate(targets):
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(targets)
    executor.loader.build_graph()
    return executor.loader.project_state(targets).apps


@pytest.fixture
def at_0008():
    """Rewound to just before the conversion, with the route table still there."""
    apps = _migrate(BEFORE)
    yield apps
    _migrate(AFTER)


def _convert():
    return _migrate(AFTER)


def test_an_email_route_becomes_an_email_destination(at_0008):
    """Two conversions in a row: a route becomes an audience on a policy, and that becomes a
    destination through the email endpoint. Both have to land."""
    route = at_0008.get_model("notifications", "NotificationRoute")
    route.objects.create(event_key="run.approved", audience="submitter", transport="email")

    apps = _convert()
    destination = apps.get_model("notifications", "PolicyDestination").objects.get(
        policy__event_key="run.approved", endpoint__kind="email")

    assert (destination.audience, destination.kind) == ("submitter", "person")


def test_two_audiences_on_one_event_become_one_policy(at_0008):
    """The grain the whole change is about: two rows in, one row out."""
    route = at_0008.get_model("notifications", "NotificationRoute")
    for audience in ("submitter", "reviewers"):
        route.objects.create(event_key="run.approved", audience=audience, transport="email")

    apps = _convert()
    policies = apps.get_model("notifications", "NotificationPolicy").objects.filter(
        event_key="run.approved")

    assert policies.count() == 1
    assert sorted(d.audience for d in policies.get().destinations.all()) == [
        "reviewers", "submitter"]


def test_a_direct_message_route_becomes_a_direct_message_destination(at_0008):
    endpoint_model = at_0008.get_model("notifications", "WebhookEndpoint")
    route = at_0008.get_model("notifications", "NotificationRoute")
    endpoint = endpoint_model.objects.create(
        name="mm", url="https://chat.invalid/hooks/abc", kind="mattermost",
    )
    route.objects.create(event_key="run.approved", audience="submitter",
                         transport="chat_dm", endpoint=endpoint)

    apps = _convert()
    destination = apps.get_model("notifications", "PolicyDestination").objects.get()

    assert destination.kind == "person"
    assert destination.audience == "submitter"
    assert destination.endpoint.name == "mm"


def test_a_post_route_becomes_a_post_with_its_channel(at_0008):
    endpoint_model = at_0008.get_model("notifications", "WebhookEndpoint")
    route = at_0008.get_model("notifications", "NotificationRoute")
    endpoint = endpoint_model.objects.create(
        name="mm", url="https://chat.invalid/hooks/abc", kind="mattermost",
    )
    route.objects.create(event_key="run.submitted", audience="reviewers",
                         transport="post", endpoint=endpoint, channel="hardware-review")

    apps = _convert()
    destination = apps.get_model("notifications", "PolicyDestination").objects.get()

    assert (destination.policy.event_key, destination.channel, destination.audience) == (
        "run.submitted", "hardware-review", "reviewers")
    assert destination.kind == "room"


def test_a_disabled_route_does_not_come_back_enabled(at_0008):
    """An admin who turned something off has to stay having turned it off."""
    route = at_0008.get_model("notifications", "NotificationRoute")
    route.objects.create(event_key="run.approved", audience="submitter",
                         transport="email", enabled=False)

    apps = _convert()

    assert not apps.get_model("notifications", "PolicyDestination").objects.filter(
        policy__event_key="run.approved").exists()


def test_a_catch_all_route_is_expanded_to_each_event_it_covered(at_0008):
    """A policy is per event by definition, so ``*`` cannot survive as itself."""
    route = at_0008.get_model("notifications", "NotificationRoute")
    route.objects.create(event_key="run.approved", audience="submitter", transport="email")
    route.objects.create(event_key="run.rejected", audience="submitter", transport="email")
    route.objects.create(event_key="*", audience="reviewers", transport="email")

    apps = _convert()
    policy_model = apps.get_model("notifications", "NotificationPolicy")
    destination_model = apps.get_model("notifications", "PolicyDestination")

    for key in ("run.approved", "run.rejected"):
        assert destination_model.objects.filter(
            policy__event_key=key, audience="reviewers").exists()
    assert not policy_model.objects.filter(event_key="*").exists()
