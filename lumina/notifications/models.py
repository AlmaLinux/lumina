"""Outbox-backed notifications: events queued in-transaction, delivered out of band.

A state change that someone needs to act on writes one ``NotificationEvent`` inside its own DB
transaction (so it commits atomically with the change and is discarded on rollback), and returns.
The ``deliver_notifications`` management command, run every minute by a systemd timer, fans each
event out to its audience's email addresses and any subscribed webhook endpoints and delivers them -
all the slow SMTP/HTTP work out of the request path. Nothing here blocks a submission or a render.
"""
from __future__ import annotations

import secrets
from typing import override

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models

from lumina.core.models import URL_MAX_LENGTH

# The audiences ``services._users_for`` knows how to resolve. Kept in step with
# lumina.notifications.events, which names the same three.
AUDIENCE_CHOICES = [
    ("reviewers", "Reviewers (the reviewer groups, plus LUMINA_REVIEW_NOTIFY_EMAILS)"),
    ("submitter", "The person the object belongs to"),
    ("vendor_members", "Submit-role members of the owning vendor"),
]
AUDIENCES = [value for value, _label in AUDIENCE_CHOICES]


class NotificationEndpoint(models.Model):
    """Somewhere notifications can go: a mailbox, a chat workspace, a webhook consumer.

    Was ``NotificationEndpoint``, and email was not one of them - it sat on the policy as its own field,
    which made the built-in transport the one thing shaped differently from all the others. Email
    is an endpoint here like any other, so a policy is an event and a list of destinations with no
    special case in it.

    A destination, not a subscription. It used to carry its own ``event_keys`` list and a
    ``direct_messages`` flag, which meant the same list drove both a channel post and a direct
    message, so an endpoint configured for both sent every personal event twice. Which events reach
    it, who they are about, and whether they arrive at a person or a room are all the event's
    ``NotificationPolicy`` now, so there is one place to look and one place to change.

    Global, not per-vendor. The signing ``secret`` is stored retrievably - unlike an API token, a
    webhook secret must be handed back to the receiver so it can verify the signature - and is shown
    in the admin.
    """

    KIND_EMAIL = "email"
    KIND_GENERIC = "generic"
    KIND_MATTERMOST = "mattermost"
    KIND_CHOICES = [
        (KIND_EMAIL, "Email"),
        (KIND_MATTERMOST, "Mattermost incoming webhook"),
        (KIND_GENERIC, "Generic (HMAC-signed event JSON)"),
    ]
    # Which kinds can address a person, and which can address a shared place. Email is people only,
    # deliberately: a group alias is one, and treating it as a room would put personal outcomes in
    # a shared mailbox. A generic consumer is the reverse - one receiver, nobody to name.
    _ADDRESSES_PEOPLE = {KIND_EMAIL, KIND_MATTERMOST}
    _ADDRESSES_ROOMS = {KIND_MATTERMOST, KIND_GENERIC}
    # The platforms with rooms to choose between, which is what a channel override is for.
    _CHAT = {KIND_MATTERMOST}

    name = models.CharField(max_length=120, help_text="A label for this endpoint.")
    url = models.URLField(
        max_length=URL_MAX_LENGTH, blank=True,
        help_text="Where the POST is sent. Blank for email, which has no URL.",
    )
    kind = models.CharField(
        max_length=16, choices=KIND_CHOICES, default=KIND_GENERIC,
        help_text="Generic posts the signed event JSON. Mattermost posts a chat message to an "
                  "incoming-webhook URL - no signature, the unguessable URL is the secret.",
    )
    secret = models.CharField(
        max_length=64, blank=True,
        help_text="Generic only: the shared secret the receiver verifies the X-Lumina-Signature "
                  "HMAC against. Generated on first save if left blank; unused for Mattermost.",
    )
    enabled = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    last_delivery_at = models.DateTimeField(null=True, blank=True)
    last_status = models.CharField(max_length=40, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.name

    @override
    def save(self, *args, **kwargs):
        # Only the generic kind signs, so only it needs a secret; a Mattermost URL is its own secret.
        if self.kind == self.KIND_GENERIC and not self.secret:
            self.secret = secrets.token_urlsafe(32)
        super().save(*args, **kwargs)

    @property
    def is_chat(self) -> bool:
        """Whether this platform has rooms to choose between, which a channel override needs.

        The one place a new chat platform has to be named: Slack beside Mattermost is a kind, a
        payload builder, and these three sets, and everything above works on it unchanged.
        """
        return self.kind in self._CHAT

    @property
    def can_address_people(self) -> bool:
        return self.kind in self._ADDRESSES_PEOPLE

    @property
    def can_address_rooms(self) -> bool:
        return self.kind in self._ADDRESSES_ROOMS



class NotificationEvent(models.Model):
    """One occurrence of a notifiable event - the outbox row written in-transaction by ``emit``.

    Minimal on purpose: it names the event and points at the object, and the drainer renders content
    from the live target at delivery time. ``processed_at`` is set once the event has been fanned out
    into per-recipient ``NotificationDelivery`` rows, so a second drain does not fan it out twice.
    """

    event_key = models.CharField(max_length=60, db_index=True)
    target_content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE, related_name="+")
    target_id = models.PositiveBigIntegerField()
    target = GenericForeignKey("target_content_type", "target_id")
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    processed_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [models.Index(fields=["target_content_type", "target_id"])]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.event_key} #{self.pk}"


class NotificationDelivery(models.Model):
    """One attempt-tracked delivery of an event to one destination - an email address or an endpoint.

    Concurrency- and retry-safe without holding a lock across the slow send: the drainer claims a due
    delivery with a compare-and-set ``UPDATE`` (bumping ``attempts`` and pushing ``next_attempt_at``
    into the future as a lease), sends outside any transaction, then records the outcome.
    """

    CHANNEL_EMAIL = "email"
    CHANNEL_WEBHOOK = "webhook"
    # A direct message to one person, sent through a Mattermost incoming webhook by
    # overriding its channel to "@handle". A separate channel from ``webhook`` because it
    # is addressed at a person rather than at a room: it retries, fails, and is audited
    # per recipient.
    CHANNEL_CHAT = "chat"
    CHANNEL_CHOICES = [
        (CHANNEL_EMAIL, "Email"),
        (CHANNEL_WEBHOOK, "Webhook"),
        (CHANNEL_CHAT, "Chat direct message"),
    ]

    STATUS_PENDING = "pending"
    STATUS_SENT = "sent"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"), (STATUS_SENT, "Sent"), (STATUS_FAILED, "Failed"),
    ]

    event = models.ForeignKey(NotificationEvent, on_delete=models.CASCADE, related_name="deliveries")
    channel = models.CharField(max_length=10, choices=CHANNEL_CHOICES)
    email = models.EmailField(blank=True)
    endpoint = models.ForeignKey(
        NotificationEndpoint, on_delete=models.CASCADE, null=True, blank=True, related_name="deliveries",
    )
    # Where in the chat platform this goes: a person's handle for a direct message (without
    # the @), or a channel name for a room post that overrides the one the incoming webhook
    # is locked to. Blank means the endpoint's own default. Kept beside ``email`` rather than
    # reusing it because a chat destination is not an address anybody can mail, and the two
    # get confused the moment they share a column.
    handle = models.CharField(max_length=150, blank=True)
    # Which audience this delivery was created for, from the route that made it. Needed at send
    # time rather than derivable from the event: it decides where the link points, and one event
    # can now reach a reviewer channel and its submitter at once. Not part of the uniqueness rule,
    # so somebody who is both a reviewer and the submitter gets one message, not two.
    audience = models.CharField(max_length=16, blank=True)
    status = models.CharField(
        max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True,
    )
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at"]
        # Django would otherwise pluralize the model name as "notification deliverys".
        verbose_name_plural = "notification deliveries"
        constraints = [
            # Fan-out uses get_or_create under an event-level lock, so this is a backstop. Fully
            # enforced for webhooks; email rows (endpoint IS NULL, distinct under SQL null semantics)
            # lean on that lock + get_or_create instead.
            models.UniqueConstraint(
                fields=["event", "channel", "email", "endpoint", "handle"],
                name="notification_delivery_unique_destination",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.channel} -> {self.email or self.handle or self.endpoint} ({self.status})"


class NotificationPolicy(models.Model):
    """One event, and the list of places it goes.

    Replaces a table of one row per (event, audience, transport, endpoint) combination. That shape
    was right about the model - routing is data an admin owns, not a constant in code - and wrong
    about the grain: the event key was repeated on every row, a single event commonly took four of
    them, and answering "what does this event do" meant reading a filtered list rather than a page.

    One row per event, and the destinations that genuinely can repeat - a post can go to several
    rooms, each with its own channel - hang off it as ``posts``. An event with no policy, or with a
    disabled one, reaches nobody: that is the off switch, and the registry still only says what an
    event *is*.
    """

    event_key = models.CharField(
        max_length=60, unique=True,
        help_text="The event this policy covers. See the notification event registry.",
    )
    enabled = models.BooleanField(default=True)

    class Meta:
        ordering = ["event_key"]
        verbose_name_plural = "notification policies"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.event_key




class PolicyDestination(models.Model):
    """One place an event goes: an endpoint, and whether it is addressed at people or at a room.

    Was three things. A policy carried an ``email_audiences`` list, a single ``dm_endpoint``, and a
    separate list of rooms, which said several wrong things at once: that email was shaped
    differently from every other transport, that direct messages had exactly one destination while
    rooms could have many, and that addressing a person and addressing a room were different in
    kind when they are the same delivery to a different sort of address. It also made "message both
    Slack and Mattermost" impossible to express.

    One list, therefore, and a row says where it goes and who it is for. Several rows per policy is
    the point: mail the submitter, message them on two platforms, post to a room, or all of it.
    """

    KIND_PERSON = "person"
    KIND_ROOM = "room"
    KIND_CHOICES = [
        (KIND_PERSON, "To each person in the audience (an email, or a chat direct message)"),
        (KIND_ROOM, "To a shared place (a chat channel, or a webhook consumer)"),
    ]

    policy = models.ForeignKey(
        NotificationPolicy, on_delete=models.CASCADE, related_name="destinations",
    )
    endpoint = models.ForeignKey(
        NotificationEndpoint, on_delete=models.CASCADE, related_name="destinations",
    )
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default=KIND_PERSON)
    channel = models.CharField(
        max_length=120, blank=True,
        help_text="Chat rooms only: the channel to post in, overriding the one the incoming "
                  "webhook is locked to. Blank uses the webhook's own channel.",
    )
    audience = models.CharField(
        max_length=16, choices=AUDIENCE_CHOICES, default="reviewers",
        help_text="Who this is for. Addressed at a person it decides who hears; addressed at a "
                  "room it decides where the link points, since a reviewer message should open "
                  "the queue and a personal one the object itself.",
    )
    enabled = models.BooleanField(default=True)

    class Meta:
        ordering = ["endpoint", "kind", "channel"]
        constraints = [
            models.UniqueConstraint(
                fields=["policy", "endpoint", "kind", "channel", "audience"],
                name="notification_destination_unique",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        where = "each person" if self.kind == self.KIND_PERSON else (self.channel or "its channel")
        return f"{self.endpoint} ({where})"

    @override
    def clean(self) -> None:
        from django.core.exceptions import ValidationError

        if self.kind == self.KIND_PERSON:
            if self.channel:
                raise ValidationError({"channel": "A person is not a room."})
            if not self.endpoint.can_address_people:
                raise ValidationError(
                    {"endpoint": f"{self.endpoint.get_kind_display()} has no people to address."})
            return
        if not self.endpoint.can_address_rooms:
            raise ValidationError(
                {"endpoint": f"{self.endpoint.get_kind_display()} has no rooms to post to."})
        if self.channel and not self.endpoint.is_chat:
            raise ValidationError(
                {"channel": "Only a chat endpoint has channels to choose between."})
