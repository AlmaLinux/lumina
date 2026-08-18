"""Admin for notification config + observability.

Two things admins manage: ``NotificationEndpoint`` registers somewhere notifications can go - a
mailbox, a chat workspace, a webhook consumer - and ``NotificationPolicy`` says, per event, which
of those it reaches and who each one is for. The event/delivery tables are read-only windows for
debugging what fired and what was delivered.
"""
from __future__ import annotations

from django import forms
from django.contrib import admin
from unfold.admin import ModelAdmin, TabularInline

from lumina.notifications import events, provisioning
from lumina.notifications.models import (
    NotificationDelivery,
    NotificationEndpoint,
    NotificationEvent,
    NotificationPolicy,
    PolicyDestination,
)


class NotificationPolicyForm(forms.ModelForm):
    """Pick the event from the live registry rather than typing a key that may not exist."""

    event_key = forms.ChoiceField(help_text="The event this policy covers.")

    class Meta:
        model = NotificationPolicy
        # Every editable field, and a test holds it to that: this list is explicit, so a field
        # added to the model is invisible here until it is named. ``direct_messages`` on the old
        # endpoint was added and missed exactly that way, which made the feature unconfigurable
        # while looking finished.
        fields = ["event_key", "enabled"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["event_key"].choices = [
            (event.key, f"{event.key} - {event.description}") for event in events.EVENTS.values()
        ]


class PolicyDestinationInline(TabularInline):
    """Everywhere an event goes, on the event's own page.

    An inline rather than a table of its own: destinations are the one part of a policy that
    repeats, and reading them beside the rest is the whole point of a policy being one row.

    ``extra = 0`` so the table opens with the destinations that exist and nothing else. A blank row
    prefilled with defaults reads as a destination somebody configured, and on a policy that mails
    one audience it said there was a second one that did nothing. "Add another" is the way to get
    an empty row, which is when an empty row means something.
    """

    model = PolicyDestination
    extra = 0
    fields = ("endpoint", "kind", "channel", "audience", "enabled")


@admin.register(NotificationPolicy)
class NotificationPolicyAdmin(ModelAdmin):
    """What one event does: everywhere it goes, and who each of those is for.

    An event with no policy here, or a disabled one, reaches nobody. That is the off switch: the
    registry says what an event is, this says who is told.
    """

    form = NotificationPolicyForm
    inlines = [PolicyDestinationInline]
    list_display = ("event_key", "people", "rooms", "enabled")
    list_filter = ("enabled",)
    search_fields = ("event_key",)
    list_editable = ("enabled",)

    @admin.display(description="To people")
    def people(self, obj) -> str:
        return ", ".join(
            f"{d.endpoint.name}: {d.audience}"
            for d in obj.destinations.all()
            if d.enabled and d.kind == PolicyDestination.KIND_PERSON
        ) or "-"

    @admin.display(description="To rooms")
    def rooms(self, obj) -> str:
        """Named in the changelist because "why did that not fire" is asked here first."""
        return ", ".join(
            f"{d.endpoint.name}{'/' + d.channel if d.channel else ''}"
            for d in obj.destinations.all()
            if d.enabled and d.kind == PolicyDestination.KIND_ROOM
        ) or "-"


@admin.register(NotificationEndpoint)
class NotificationEndpointAdmin(ModelAdmin):
    """Somewhere notifications can go: email, a Mattermost webhook, or a signed-JSON consumer.

    What it receives is a ``NotificationRoute`` question, not an endpoint one. The signing
    ``secret`` is shown (not hashed): the receiver needs it to verify the signature. Leave it blank
    on a new endpoint and one is generated on save.
    """

    list_display = ("name", "kind", "url", "enabled", "configured_by", "routes_using",
                    "last_status", "last_delivery_at", "created_at")
    list_filter = ("enabled", "kind")
    search_fields = ("name", "url")
    autocomplete_fields = ("created_by",)
    readonly_fields = ("configured_by", "created_at", "last_delivery_at", "last_status")
    fields = ("name", "url", "kind", "secret", "enabled", "configured_by", "created_by",
              "created_at", "last_delivery_at", "last_status")

    @admin.display(description="Configured by")
    def configured_by(self, obj) -> str:
        """Who owns this row: this page, or the deployment.

        A deployment-configured endpoint has its URL and its rooms rewritten by every deploy, so
        an edit made here to those does not last. Everything else about it does, ``enabled``
        included: switching one off is a decision, and a deploy does not undo it.
        """
        if obj is not None and provisioning.is_deployment_managed(obj):
            return "the deployment - its URL and rooms are rewritten on each deploy"
        return "this page"

    @admin.display(description="Used by")
    def routes_using(self, obj) -> str:
        """How many policies send anything here.

        In the changelist because "why did that not fire" is answered here more often than
        anywhere else, and an endpoint nothing points at is configured and silent.
        """
        live = obj.destinations.filter(enabled=True)
        return f"{live.filter(kind=PolicyDestination.KIND_PERSON).count()} people, " \
               f"{live.filter(kind=PolicyDestination.KIND_ROOM).count()} rooms"


class DeliveryInline(TabularInline):
    model = NotificationDelivery
    extra = 0
    can_delete = False
    fields = ("channel", "audience", "email", "handle", "endpoint", "status", "attempts",
              "next_attempt_at", "sent_at",
              "last_error")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(NotificationEvent)
class NotificationEventAdmin(ModelAdmin):
    list_display = ("event_key", "target_content_type", "target_id", "actor", "created_at",
                    "processed_at")
    list_filter = ("event_key", "processed_at")
    date_hierarchy = "created_at"
    inlines = [DeliveryInline]

    def has_add_permission(self, request):
        return False


@admin.register(NotificationDelivery)
class NotificationDeliveryAdmin(ModelAdmin):
    list_display = ("event", "channel", "audience", "email", "handle", "endpoint", "status",
                    "attempts",
                    "next_attempt_at", "sent_at")
    list_filter = ("channel", "status")
    search_fields = ("email", "last_error")
    readonly_fields = ("event", "channel", "audience", "email", "handle", "endpoint",
                       "created_at", "sent_at")

    def has_add_permission(self, request):
        return False
