"""Seed the routing table from what each endpoint was already subscribed to.

Not a faithful copy of the old behavior, deliberately. Two of the things routes exist to fix were
happening every day and reproducing them here would mean shipping them again:

- A channel post ignored the audience, so a room subscribed to ``submission.approved`` published
  somebody's personal outcome. Mattermost endpoints are seeded with post routes for reviewer
  events only.
- An endpoint that could direct-message *also* received the channel post for the same event, since
  one ``event_keys`` list drove both. A DM-capable endpoint is seeded with direct messages for
  personal events and posts for reviewer events, never both for one event.

A generic endpoint is a programmatic consumer rather than a room, so it keeps a post route for
every key it had, whatever the audience.

The event-to-audience map is a snapshot rather than an import of the registry: a data migration has
to keep meaning what it meant, and the registry will move on.
"""
from django.db import migrations

# The registry as it stood when routing became data.
AUDIENCES = {
    "run.needs_details": "submitter",
    "run.submitted": "reviewers",
    "run.needs_changes": "submitter",
    "run.rejected": "submitter",
    "run.approved": "submitter",
    "run.published": "submitter",
    "submission.created": "reviewers",
    "submission.needs_changes": "submitter",
    "submission.rejected": "submitter",
    "submission.approved": "submitter",
    "vendor_claim.submitted": "reviewers",
    "vendor_claim.decided": "submitter",
    "survey_token_request.submitted": "reviewers",
    "survey_token_request.decided": "submitter",
    "survey.submitted": "reviewers",
    "survey_submission.decided": "submitter",
    "proposal.created": "reviewers",
    "proposal.decided": "submitter",
}


def seed(apps, schema_editor):
    route_model = apps.get_model("notifications", "NotificationRoute")
    endpoint_model = apps.get_model("notifications", "WebhookEndpoint")

    # Email to the event's own audience, which is what every event did before routes existed.
    for key, audience in AUDIENCES.items():
        route_model.objects.get_or_create(
            event_key=key, audience=audience, transport="email", endpoint=None, channel="",
            defaults={"enabled": True},
        )

    for endpoint in endpoint_model.objects.all():
        mattermost = endpoint.kind == "mattermost"
        for key in endpoint.event_keys or []:
            audience = AUDIENCES.get(key)
            if audience is None:
                continue  # a key that is no longer an event
            dm = mattermost and endpoint.direct_messages and audience != "reviewers"
            if mattermost and not dm and audience != "reviewers":
                continue  # a personal outcome does not belong in a shared room
            route_model.objects.get_or_create(
                event_key=key, audience=audience,
                transport="chat_dm" if dm else "post",
                endpoint=endpoint, channel="", defaults={"enabled": endpoint.enabled},
            )


def unseed(apps, schema_editor):
    apps.get_model("notifications", "NotificationRoute").objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [("notifications", "0005_notificationroute_and_delivery_audience")]

    operations = [migrations.RunPython(seed, unseed)]
