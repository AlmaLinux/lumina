"""Admin for notification config + observability.

``WebhookEndpoint`` is the one thing admins actively manage - register a URL, tick the events it
should receive. The event/delivery tables are read-only windows for debugging what fired and what
was delivered.
"""
from __future__ import annotations

from django import forms
from django.contrib import admin
from unfold.admin import ModelAdmin, TabularInline

from lumina.notifications import events
from lumina.notifications.models import (
    NotificationDelivery,
    NotificationEvent,
    WebhookEndpoint,
)


class WebhookEndpointForm(forms.ModelForm):
    """Subscribe an endpoint by ticking events from the registry, not by hand-typing a JSON list."""

    event_keys = forms.MultipleChoiceField(
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text="Events this endpoint receives an HMAC-signed POST for.",
    )

    class Meta:
        model = WebhookEndpoint
        # Every editable field, and a test below holds it to that: this list is explicit,
        # so a field added to the model is invisible in the admin until it is named here.
        # ``direct_messages`` was added and missed exactly that way, which made the DM
        # feature unconfigurable while looking finished.
        fields = ["name", "url", "kind", "secret", "event_keys", "enabled",
                  "direct_messages", "created_by"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Set from the live registry so a new event shows up here the moment it is declared.
        self.fields["event_keys"].choices = [
            (event.key, f"{event.key} - {event.description}") for event in events.EVENTS.values()
        ]
        # Name what this endpoint is NOT subscribed to. Subscription is explicit - an
        # endpoint receives exactly the keys it lists - so an event added after the
        # endpoint was configured never reaches it, and there was nothing anywhere that
        # said so: the endpoint looked configured and simply did not fire. Auto-subscribing
        # would be worse, since it would change what a configured endpoint sends without
        # anybody asking, so the answer is to make the gap visible.
        missing = [
            key for key in events.EVENTS
            if key not in (getattr(self.instance, "event_keys", None) or [])
        ]
        if self.instance.pk and missing:
            self.fields["event_keys"].help_text = (
                f"Events this endpoint receives an HMAC-signed POST for. "
                f"It is NOT subscribed to {len(missing)} of {len(events.EVENTS)}: "
                f"{', '.join(missing)}."
            )


@admin.register(WebhookEndpoint)
class WebhookEndpointAdmin(ModelAdmin):
    """A global endpoint that receives HMAC-signed POSTs for the event keys it subscribes to.

    The signing ``secret`` is shown (not hashed): the receiver needs it to verify the signature.
    Leave it blank on a new endpoint and one is generated on save.
    """

    form = WebhookEndpointForm
    list_display = ("name", "kind", "url", "enabled", "subscribed", "last_status",
                    "last_delivery_at", "created_at")
    list_filter = ("enabled", "kind")
    search_fields = ("name", "url")
    autocomplete_fields = ("created_by",)
    readonly_fields = ("created_at", "last_delivery_at", "last_status")

    @admin.display(description="Events")
    def subscribed(self, obj) -> str:
        """How many of the registry's events this endpoint receives.

        In the changelist because "why did that not fire" is answered here more often than
        anywhere else: an endpoint reading 9 of 12 is one that missed three events added
        since somebody last looked at it.
        """
        return f"{len(obj.event_keys or [])} of {len(events.EVENTS)}"


class DeliveryInline(TabularInline):
    model = NotificationDelivery
    extra = 0
    can_delete = False
    fields = ("channel", "email", "endpoint", "status", "attempts", "next_attempt_at", "sent_at",
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
    list_display = ("event", "channel", "email", "endpoint", "status", "attempts",
                    "next_attempt_at", "sent_at")
    list_filter = ("channel", "status")
    search_fields = ("email", "last_error")
    readonly_fields = ("event", "channel", "email", "endpoint", "created_at", "sent_at")

    def has_add_permission(self, request):
        return False
