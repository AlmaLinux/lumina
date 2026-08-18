"""Notification routing the deployment owns, rather than the admin.

Endpoints and policies are ordinary rows, which is where they belong: a reviewer can add a chat
room without a deploy. The cost is that they live only in the database, so a rebuilt one comes back
with no chat notifications at all and somebody has to remember to add them again - on the dev host
that is every reset, and in production it is any restore that is not a full dump.

So one endpoint is configured by the deployment instead. The incoming-webhook URL arrives as an
environment variable (a GitHub secret on dev, vault in production) and ``configure_mattermost``
reconciles the rows to match it. Idempotent, so it runs on every deploy and a rotated URL is picked
up by the next one.

What it owns and what it leaves alone: it keeps its own endpoint's URL and channel in step, and
makes sure every reviewer event posts there. It never touches ``enabled`` on a row that already
exists, so silencing a noisy endpoint or one of its rooms in the admin survives a deploy. And it
routes only the reviewer events - a channel is a room, and "your run was rejected" is not news for
a room.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from django.conf import settings
from django.core.validators import URLValidator
from django.db import transaction

from lumina.notifications import events
from lumina.notifications.models import (
    NotificationEndpoint,
    NotificationPolicy,
    PolicyDestination,
)

# What the deployment's own Mattermost endpoint is found by, so a changed URL updates that row
# rather than adding a second one posting the same thing twice. A constant rather than a setting:
# an installation that wants two Mattermost endpoints adds the second in the admin, which this
# leaves alone. Renaming this one in the admin does make the next deploy create another, which is
# the trade for not carrying a "managed by the deployment" column nothing else would use.
MATTERMOST_ENDPOINT_NAME = "Mattermost"

# http and https only. The default validator also accepts ftp, which no webhook consumer speaks.
_validate_url = URLValidator(schemes=["http", "https"])


def is_deployment_managed(endpoint: NotificationEndpoint) -> bool:
    """Whether a reconcile owns this row, and would write over an edit made to it in the admin.

    Derived rather than stored: the endpoint is found by its kind and its name, so those two plus
    a configured URL are exactly what makes it the deployment's. The admin shows the answer, since
    learning it by watching a URL change back after a deploy is a bad afternoon.
    """
    return bool(
        getattr(settings, "LUMINA_MATTERMOST_WEBHOOK_URL", "").strip()
        and endpoint.kind == NotificationEndpoint.KIND_MATTERMOST
        and endpoint.name == MATTERMOST_ENDPOINT_NAME
    )


@dataclass
class Outcome:
    """What one reconcile did: reported by the command, asserted on by the tests."""

    endpoint: NotificationEndpoint | None = None
    created: bool = False
    url_changed: bool = False
    routed: list[str] = field(default_factory=list)  # events that now post there and did not
    rechanneled: list[str] = field(default_factory=list)  # events moved to a different channel


def deployment_events() -> list[str]:
    """The events a deployment-configured room is subscribed to: the ones reviewers must act on.

    Read off the registry rather than listed here, so an event added in a release is routed by the
    next deploy instead of being quietly unannounced. ``webhookable`` is honoured because fan-out
    honours it: a destination for an event that never leaves the building would be a row that can
    only ever be skipped.
    """
    return [
        key for key, event in events.EVENTS.items()
        if event.audience == events.REVIEWERS and event.webhookable
    ]


@transaction.atomic
def configure_mattermost(*, url: str, channel: str = "") -> Outcome:
    """Reconcile the deployment's Mattermost endpoint and the reviewer events that post to it.

    A blank URL configures nothing and is not an error: it is how an installation says it has no
    chat, and deleting whatever an admin had set up would be a surprising way to read that.
    """
    url = url.strip()
    # Mattermost wants the channel's name, the way it appears in the URL bar. Written with a
    # leading "#" it is not that name and the post is rejected, so take one off if it is there.
    channel = channel.strip().lstrip("#")
    if not url:
        return Outcome()
    # Raises ValidationError, which the command turns into a failed deploy. A typo here would
    # otherwise be stored and only surface later as every delivery failing its five attempts.
    _validate_url(url)

    endpoint = NotificationEndpoint.objects.filter(
        kind=NotificationEndpoint.KIND_MATTERMOST, name=MATTERMOST_ENDPOINT_NAME,
    ).first()
    outcome = Outcome(endpoint=endpoint)
    if endpoint is None:
        outcome.endpoint = NotificationEndpoint.objects.create(
            name=MATTERMOST_ENDPOINT_NAME, kind=NotificationEndpoint.KIND_MATTERMOST, url=url,
        )
        outcome.created = True
    elif endpoint.url != url:
        endpoint.url = url
        endpoint.save(update_fields=["url"])
        outcome.url_changed = True

    for event_key in deployment_events():
        _route(outcome, event_key, channel)
    return outcome


def _route(outcome: Outcome, event_key: str, channel: str) -> None:
    """Make sure this event posts to the endpoint, in the configured channel.

    The channel is part of what makes a destination unique, so creating one unconditionally would
    give a changed channel two rows and post everything twice. An existing post is moved instead.
    """
    policy, _ = NotificationPolicy.objects.get_or_create(event_key=event_key)
    posts = PolicyDestination.objects.filter(
        policy=policy, endpoint=outcome.endpoint, kind=PolicyDestination.KIND_ROOM,
        audience=events.REVIEWERS,
    )
    if posts.filter(channel=channel).exists():
        return
    moved = posts.order_by("pk").first()
    if moved is not None:
        moved.channel = channel
        moved.save(update_fields=["channel"])
        outcome.rechanneled.append(event_key)
        return
    PolicyDestination.objects.create(
        policy=policy, endpoint=outcome.endpoint, kind=PolicyDestination.KIND_ROOM,
        audience=events.REVIEWERS, channel=channel,
    )
    outcome.routed.append(event_key)
