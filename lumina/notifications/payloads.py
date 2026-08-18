"""Turning an event into the body one chat platform wants.

One builder per ``WebhookEndpoint.kind``, chosen by ``for_endpoint``. Mattermost is the
only rich one today; the generic kind stays a signed JSON envelope for anything consuming
lumina programmatically. A new platform is a new function and a row in the registry at the
bottom, which is the reason ``kind`` exists on the endpoint at all.

Mattermost has two rich formats and this uses the older one deliberately:

- **Slack-compatible attachments** (used here): a coloured bar, a linked title, and
  labelled fields. Supported by every Mattermost version that has ever had incoming
  webhooks, and it degrades to the plain ``text`` fallback if anything cannot render it.
- **``props.mm_blocks``**: newer and considerably richer, with buttons and menus. Its
  interactive parts need an action endpoint for Mattermost to call back into, which lumina
  does not have - a button reading "Approve" would need an authenticated, CSRF-safe
  handler and a decision about who is allowed to press it from chat. Worth doing, but that
  is a feature rather than a payload change, and until then blocks would buy a layout at
  the cost of requiring a recent server.
"""
from __future__ import annotations

from lumina.notifications import events
from lumina.notifications.models import WebhookEndpoint

# Mattermost renders these as the attachment's left bar. Named tones map to hex here and
# nowhere else, so another platform can map the same tones to whatever it uses.
_TONE_COLORS = {
    events.TONE_INFO: "#0069da",       # AlmaLinux Science blue
    events.TONE_ATTENTION: "#e1ad00",  # brand yellow, darkened for a light background
    events.TONE_GOOD: "#68bc11",       # brand green
    events.TONE_BAD: "#e1282b",        # brand red
}

_FIELD_LIMIT = 10


def mattermost(event, *, url: str, fields: list[dict] | None = None) -> dict:
    """A rich Mattermost message: fallback text plus one coloured attachment.

    ``text`` is set as well as the attachment, and not only for redundancy: it is what
    Mattermost shows in a push notification and in the channel sidebar preview, where an
    attachment is not rendered at all. A reader on a phone sees the subject either way.
    """
    ev = events.get(event.event_key)
    subject = ev.subject if ev else event.event_key
    tone = ev.tone if ev else events.TONE_INFO

    attachment = {
        # Shown by clients that cannot render an attachment, and in notifications.
        "fallback": subject,
        "color": _TONE_COLORS.get(tone, _TONE_COLORS[events.TONE_INFO]),
        "title": subject,
        "text": ev.description if ev and ev.description else "",
        "footer": "AlmaLinux hardware certification",
    }
    if url:
        attachment["title_link"] = url
    if fields:
        # Capped so one unusually detailed target cannot post a wall of rows.
        attachment["fields"] = fields[:_FIELD_LIMIT]
    return {"text": f"**{subject}**", "attachments": [attachment]}


def generic(event, *, url: str, fields: list[dict] | None = None) -> dict:
    """The machine-readable envelope, for anything that is not a chat platform.

    Unchanged in shape, because it is signed and somebody may already be parsing it.
    """
    return {
        "event": event.event_key,
        "id": event.pk,
        "created_at": event.created_at.isoformat(),
        "target": {
            "type": event.target_content_type.model,
            "id": event.target_id,
            "url": url,
        },
        "actor": event.actor.get_username() if event.actor else None,
        # The same facts the chat attachment shows, so a generic consumer is not the
        # poor relation.
        "fields": {field["title"]: field["value"] for field in (fields or [])},
    }


BUILDERS = {
    WebhookEndpoint.KIND_MATTERMOST: mattermost,
    WebhookEndpoint.KIND_GENERIC: generic,
}


def for_endpoint(kind: str):
    """The payload builder for an endpoint kind, falling back to the signed envelope."""
    return BUILDERS.get(kind, generic)


def target_fields(target) -> list[dict]:
    """Labelled facts about the object, for a rich message.

    Opt-in by the model: anything defining ``notification_fields()`` returns its own
    ``[(label, value), ...]``, and anything that does not simply has no fields. The
    alternative was type-sniffing every notifiable model from in here, which puts
    knowledge of runs, submissions, and survey rows into the notifications app and goes
    stale silently.

    ``short`` is Mattermost's two-column hint; everything here is short enough for it.
    """
    getter = getattr(target, "notification_fields", None)
    if not callable(getter):
        return []
    try:
        pairs = getter()
    except Exception:  # a display helper must never break a delivery
        return []
    return [
        {"title": str(label), "value": str(value), "short": True}
        for label, value in pairs
        if value not in (None, "")
    ]
