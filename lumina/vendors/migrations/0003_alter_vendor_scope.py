"""``help_text`` only, from the serial-comma pass over the whole project.

No database effect: ``help_text`` lives in the migration state and nowhere in the schema. It exists
so ``makemigrations --check`` stays clean, which is a CI gate, and so the next person to touch this
model does not find an unexplained pending change waiting for them.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("vendors", "0002_pci_ids_vendor_aliases"),
    ]

    operations = [
        migrations.AlterField(
            model_name="vendor",
            name="scope",
            field=models.CharField(
                choices=[
                    ("hardware", "Hardware"),
                    ("software", "Software"),
                    ("both", "Hardware and software"),
                ],
                db_index=True,
                default="hardware",
                help_text="Which catalog this vendor appears in. Memberships, aliases, and verification are shared regardless.",
                max_length=16,
            ),
        ),
    ]
