"""The endpoint stops carrying its own subscription: routes say what reaches it."""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [("notifications", "0006_seed_notification_routes")]

    operations = [
        migrations.RemoveField(model_name="webhookendpoint", name="direct_messages"),
        migrations.RemoveField(model_name="webhookendpoint", name="event_keys"),
    ]
