"""``max_minor`` becomes ``latest_minor``, and a Kitten marker joins it.

A rename rather than a drop-and-add, which is what ``makemigrations`` proposed and would have
thrown the values away. The old field's help text already told administrators to "raise it here
when a new minor ships", so the number they have been maintaining is exactly the one the new
field needs - the newest minor of that major that has actually shipped.

Its *purpose* changed, though. It used to cap a compatibility dropdown, and hardware no longer
certifies per minor so nothing offers that dropdown. What it does now is decide whether hardware
whose enablement was proved on AlmaLinux Kitten is claimable yet.

The default drops from 10 to 0 for rows created from here on: a brand-new major has shipped no
minors, and defaulting to 10 would silently lift a disclaimer on a release that does not exist.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("releases", "0001_initial")]

    operations = [
        migrations.RenameField(
            model_name="almalinuxrelease",
            old_name="max_minor",
            new_name="latest_minor",
        ),
        migrations.AlterField(
            model_name="almalinuxrelease",
            name="latest_minor",
            field=models.PositiveSmallIntegerField(
                default=0,
                help_text=(
                    "The newest minor of this major that has actually shipped. Raise it here "
                    "when a new minor is released. This is what lifts the disclaimer on "
                    "hardware whose enablement was proved on AlmaLinux Kitten and is due to "
                    "land in a minor: while this number is lower than that minor, the catalog "
                    "says so."
                ),
            ),
        ),
        migrations.AddField(
            model_name="almalinuxrelease",
            name="kitten_target_major",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "True while AlmaLinux Kitten tracks this major. Kitten's own os-release "
                    "names a major and no minor, so this is how a Kitten run is matched to "
                    "the release it anticipates."
                ),
            ),
        ),
    ]
