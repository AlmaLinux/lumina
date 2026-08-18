"""Email becomes an endpoint like any other, and the model stops being named for webhooks."""
import django.db.models.deletion
from django.db import migrations, models

KINDS = [
    ("email", "Email"),
    ("mattermost", "Mattermost incoming webhook"),
    ("generic", "Generic (HMAC-signed event JSON)"),
]
DESTINATION_KINDS = [
    ("person", "To each person in the audience (an email, or a chat direct message)"),
    ("room", "To a shared place (a chat channel, or a webhook consumer)"),
]


class Migration(migrations.Migration):

    dependencies = [("notifications", "0013_remove_notificationpolicy_dm_audiences_and_more")]

    operations = [
        migrations.RenameModel(old_name="WebhookEndpoint", new_name="NotificationEndpoint"),
        migrations.AlterField(
            model_name="notificationendpoint",
            name="kind",
            field=models.CharField(
                choices=KINDS, default="generic",
                help_text="Generic posts the signed event JSON. Mattermost posts a chat message "
                          "to an incoming-webhook URL - no signature, the unguessable URL is the "
                          "secret.",
                max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="notificationendpoint",
            name="url",
            field=models.URLField(
                blank=True,
                help_text="Where the POST is sent. Blank for email, which has no URL.",
            ),
        ),
        migrations.AlterField(
            model_name="policydestination",
            name="kind",
            field=models.CharField(choices=DESTINATION_KINDS, default="person", max_length=10),
        ),
        migrations.AlterField(
            model_name="policydestination",
            name="endpoint",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE, related_name="destinations",
                to="notifications.notificationendpoint",
            ),
        ),
        migrations.AlterField(
            model_name="notificationdelivery",
            name="endpoint",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                related_name="deliveries", to="notifications.notificationendpoint",
            ),
        ),
    ]
