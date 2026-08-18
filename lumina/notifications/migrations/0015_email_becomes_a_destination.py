"""Each policy's ``email_audiences`` becomes destination rows through one Email endpoint.

Email was the only transport shaped differently from the rest: a list of audiences on the policy
rather than a row saying where it goes. One endpoint is created to send through, named plainly,
because a policy that mails somebody should say so the same way it says anything else.

The destination kinds are renamed in the same pass: ``dm``/``channel`` described chat, and the
question is really whether a message is addressed at people or at a shared place, which is true of
email too.
"""
from django.db import migrations

EMAIL_ENDPOINT = "Email"


def forwards(apps, schema_editor):
    endpoint_model = apps.get_model("notifications", "NotificationEndpoint")
    policy_model = apps.get_model("notifications", "NotificationPolicy")
    destination_model = apps.get_model("notifications", "PolicyDestination")

    destination_model.objects.filter(kind="dm").update(kind="person")
    destination_model.objects.filter(kind="channel").update(kind="room")

    if not policy_model.objects.exclude(email_audiences=[]).exists():
        return
    endpoint, _ = endpoint_model.objects.get_or_create(
        kind="email", defaults={"name": EMAIL_ENDPOINT, "url": "", "enabled": True},
    )
    for policy in policy_model.objects.exclude(email_audiences=[]):
        for audience in policy.email_audiences or []:
            destination_model.objects.get_or_create(
                policy=policy, endpoint=endpoint, kind="person", channel="", audience=audience,
                defaults={"enabled": True},
            )


def backwards(apps, schema_editor):
    policy_model = apps.get_model("notifications", "NotificationPolicy")
    destination_model = apps.get_model("notifications", "PolicyDestination")

    for policy in policy_model.objects.all():
        policy.email_audiences = sorted({
            d.audience for d in policy.destinations.filter(endpoint__kind="email", enabled=True)
        })
        policy.save()
    destination_model.objects.filter(endpoint__kind="email").delete()
    destination_model.objects.filter(kind="person").update(kind="dm")
    destination_model.objects.filter(kind="room").update(kind="channel")


class Migration(migrations.Migration):

    dependencies = [("notifications", "0014_notificationendpoint")]

    operations = [migrations.RunPython(forwards, backwards)]
