"""Every event mails its own audience, from the first day.

One Email endpoint, and one policy per event in the registry sending to the people that event is
about - a submitter hears about their run, reviewers hear about the queue. That is the baseline a
fresh installation needs before anybody configures a chat room or a webhook: an event with no
policy is silently dead, and ``test_every_registered_event_has_a_policy`` is the guard against a
new event being added to the registry and never routed.

Read off the registry rather than snapshotted, the same choice ``vendors`` makes for its PCI
aliases and for the same reason: this seed's purpose is "make sure these exist", so a copy of the
event list here would be the one that goes stale.
"""
from django.db import migrations

from lumina.notifications.events import EVENTS

EMAIL_ENDPOINT = "Email"


def seed(apps, schema_editor):
    endpoint_model = apps.get_model("notifications", "NotificationEndpoint")
    policy_model = apps.get_model("notifications", "NotificationPolicy")
    destination_model = apps.get_model("notifications", "PolicyDestination")

    endpoint, _ = endpoint_model.objects.get_or_create(
        kind="email", defaults={"name": EMAIL_ENDPOINT, "url": "", "enabled": True},
    )
    for key, event in EVENTS.items():
        policy, _ = policy_model.objects.get_or_create(event_key=key)
        destination_model.objects.get_or_create(
            policy=policy, endpoint=endpoint, kind="person", channel="",
            audience=event.audience, defaults={"enabled": True},
        )


def unseed(apps, schema_editor):
    apps.get_model("notifications", "PolicyDestination").objects.filter(
        endpoint__kind="email").delete()
    apps.get_model("notifications", "NotificationPolicy").objects.filter(
        event_key__in=list(EVENTS)).delete()
    apps.get_model("notifications", "NotificationEndpoint").objects.filter(kind="email").delete()


class Migration(migrations.Migration):

    dependencies = [("notifications", "0001_initial")]

    operations = [migrations.RunPython(seed, unseed)]
