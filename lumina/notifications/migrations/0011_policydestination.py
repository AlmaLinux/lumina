"""One list of destinations, replacing a single DM endpoint beside a list of rooms."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("notifications", "0010_delete_notificationroute")]

    operations = [
        migrations.RemoveConstraint(
            model_name="policypost", name="notification_post_unique_room",
        ),
        migrations.RenameModel(old_name="PolicyPost", new_name="PolicyDestination"),
        migrations.AlterModelOptions(
            name="policydestination",
            options={"ordering": ["endpoint", "kind", "channel"]},
        ),
        migrations.AddField(
            model_name="policydestination",
            name="kind",
            field=models.CharField(
                choices=[
                    ("dm", "Direct message to each person in the audience"),
                    ("channel", "Post to a channel, or to a webhook consumer"),
                ],
                default="channel", max_length=10,
            ),
        ),
        migrations.AlterField(
            model_name="policydestination",
            name="policy",
            field=models.ForeignKey(
                on_delete=models.CASCADE, related_name="destinations",
                to="notifications.notificationpolicy",
            ),
        ),
        migrations.AlterField(
            model_name="policydestination",
            name="endpoint",
            field=models.ForeignKey(
                on_delete=models.CASCADE, related_name="destinations",
                to="notifications.webhookendpoint",
            ),
        ),
        migrations.AlterField(
            model_name="policydestination",
            name="channel",
            field=models.CharField(
                blank=True,
                help_text="Channel posts only: the room to post in, overriding the one the "
                          "incoming webhook is locked to. Blank uses the webhook's own channel.",
                max_length=120,
            ),
        ),
        migrations.AlterField(
            model_name="policydestination",
            name="audience",
            field=models.CharField(
                choices=[
                    ("reviewers",
                     "Reviewers (the reviewer groups, plus LUMINA_REVIEW_NOTIFY_EMAILS)"),
                    ("submitter", "The person the object belongs to"),
                    ("vendor_members", "Submit-role members of the owning vendor"),
                ],
                default="reviewers",
                help_text="Who this is for. For a direct message it decides who is messaged; for "
                          "a channel post it decides where the link points, since a reviewer "
                          "message should open the queue and a personal one the object itself.",
                max_length=16,
            ),
        ),
        migrations.AddConstraint(
            model_name="policydestination",
            constraint=models.UniqueConstraint(
                fields=("policy", "endpoint", "kind", "channel", "audience"),
                name="notification_destination_unique",
            ),
        ),
    ]
