"""Fold the route table into one policy per event.

Routes were right that routing is data and wrong about the grain: a row per (event, audience,
transport, endpoint) repeated the event key on every row and took four rows to say what one event
does. This carries whatever an admin has configured across without asking them to retype it.

A catch-all ``*`` route is expanded into a policy per event, because a policy is per event by
definition. The ``dm_endpoint`` is the first one any direct-message route named: the field holds
one workspace, and more than one was never configurable in the same breath anyway.
"""
from django.db import migrations


def forwards(apps, schema_editor):
    route_model = apps.get_model("notifications", "NotificationRoute")
    policy_model = apps.get_model("notifications", "NotificationPolicy")
    post_model = apps.get_model("notifications", "PolicyPost")

    routes = list(route_model.objects.select_related("endpoint"))
    keys = {route.event_key for route in routes if route.event_key != "*"}
    catch_all = [route for route in routes if route.event_key == "*"]

    for key in sorted(keys):
        policy, _ = policy_model.objects.get_or_create(event_key=key)
        email: list[str] = []
        dm: list[str] = []
        for route in [r for r in routes if r.event_key == key] + catch_all:
            if not route.enabled:
                continue
            if route.transport == "email":
                if route.audience not in email:
                    email.append(route.audience)
            elif route.transport == "chat_dm":
                if route.audience not in dm:
                    dm.append(route.audience)
                if policy.dm_endpoint_id is None:
                    policy.dm_endpoint = route.endpoint
            elif route.endpoint_id is not None:
                post_model.objects.get_or_create(
                    policy=policy, endpoint=route.endpoint, channel=route.channel,
                    defaults={"audience": route.audience, "enabled": True},
                )
        policy.email_audiences = email
        policy.dm_audiences = dm
        policy.save()


def backwards(apps, schema_editor):
    """Rebuild routes from policies, so the step is reversible rather than one-way."""
    route_model = apps.get_model("notifications", "NotificationRoute")
    policy_model = apps.get_model("notifications", "NotificationPolicy")

    for policy in policy_model.objects.prefetch_related("posts").all():
        for audience in policy.email_audiences or []:
            route_model.objects.get_or_create(
                event_key=policy.event_key, audience=audience, transport="email",
                endpoint=None, channel="", defaults={"enabled": policy.enabled},
            )
        for audience in policy.dm_audiences or []:
            route_model.objects.get_or_create(
                event_key=policy.event_key, audience=audience, transport="chat_dm",
                endpoint=policy.dm_endpoint, channel="", defaults={"enabled": policy.enabled},
            )
        for post in policy.posts.all():
            route_model.objects.get_or_create(
                event_key=policy.event_key, audience=post.audience, transport="post",
                endpoint=post.endpoint, channel=post.channel,
                defaults={"enabled": policy.enabled and post.enabled},
            )


class Migration(migrations.Migration):

    dependencies = [("notifications", "0008_notificationpolicy_policypost")]

    operations = [migrations.RunPython(forwards, backwards)]
