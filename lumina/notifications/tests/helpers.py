"""Shared builders for notification tests.

Routing is data, so a test that wants an endpoint to receive something needs a policy too. Kept
here rather than repeated per module: a test that built an endpoint and forgot the policy would
pass for the wrong reason, since an unrouted endpoint is correctly silent.
"""
from __future__ import annotations

from lumina.notifications.models import (
    NotificationEndpoint,
    NotificationPolicy,
    PolicyDestination,
)


def policy_for(event_key: str, **fields) -> NotificationPolicy:
    """The policy for one event, created or updated in place."""
    policy, _ = NotificationPolicy.objects.get_or_create(event_key=event_key)
    for name, value in fields.items():
        setattr(policy, name, value)
    policy.save()
    return policy


def routed_endpoint(
    *,
    event_keys: list[str],
    audience: str = "submitter",
    channel: str = "",
    **endpoint_kwargs,
) -> NotificationEndpoint:
    """An endpoint plus a post on each event's policy, which is what "subscribed" now means."""
    endpoint = NotificationEndpoint.objects.create(**endpoint_kwargs)
    for key in event_keys:
        PolicyDestination.objects.create(
            policy=policy_for(key), endpoint=endpoint, channel=channel, audience=audience,
            kind=PolicyDestination.KIND_ROOM,
        )
    return endpoint


def dm_through(endpoint: NotificationEndpoint, event_key: str, audience: str = "submitter"):
    """Send this event's direct messages through ``endpoint``."""
    return PolicyDestination.objects.create(
        policy=policy_for(event_key), endpoint=endpoint,
        kind=PolicyDestination.KIND_PERSON, audience=audience,
    )


def email_endpoint() -> NotificationEndpoint:
    """The one email endpoint, as the data migration leaves it."""
    endpoint, _ = NotificationEndpoint.objects.get_or_create(
        kind=NotificationEndpoint.KIND_EMAIL, defaults={"name": "Email", "url": ""},
    )
    return endpoint


def email_to(event_key: str, *audiences: str) -> NotificationPolicy:
    """Mail these audiences about this event, and nobody else by mail."""
    policy = policy_for(event_key)
    policy.destinations.filter(endpoint__kind=NotificationEndpoint.KIND_EMAIL).delete()
    for audience in audiences:
        PolicyDestination.objects.create(
            policy=policy, endpoint=email_endpoint(),
            kind=PolicyDestination.KIND_PERSON, audience=audience,
        )
    return policy
