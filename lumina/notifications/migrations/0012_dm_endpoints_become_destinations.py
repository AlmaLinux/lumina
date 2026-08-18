"""The policy's single DM endpoint becomes one destination row per audience.

A direct message and a channel post are the same delivery to a different sort of address, so they
belong in one list. Carrying the old shape across rather than asking an admin to retype it, and
keeping it reversible: the reverse takes the first DM destination back to the single field, which
is all that field could ever hold.
"""
from django.db import migrations


def forwards(apps, schema_editor):
    policy_model = apps.get_model("notifications", "NotificationPolicy")
    destination_model = apps.get_model("notifications", "PolicyDestination")

    for policy in policy_model.objects.exclude(dm_endpoint__isnull=True):
        for audience in policy.dm_audiences or []:
            destination_model.objects.get_or_create(
                policy=policy, endpoint=policy.dm_endpoint, kind="dm",
                channel="", audience=audience, defaults={"enabled": True},
            )


def backwards(apps, schema_editor):
    policy_model = apps.get_model("notifications", "NotificationPolicy")

    for policy in policy_model.objects.all():
        dms = list(policy.destinations.filter(kind="dm"))
        if not dms:
            continue
        policy.dm_endpoint = dms[0].endpoint
        policy.dm_audiences = sorted({d.audience for d in dms if d.enabled})
        policy.save()
    apps.get_model("notifications", "PolicyDestination").objects.filter(kind="dm").delete()


class Migration(migrations.Migration):

    dependencies = [("notifications", "0011_policydestination")]

    operations = [migrations.RunPython(forwards, backwards)]
