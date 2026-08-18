"""``SystemKind`` loses "unknown". Custom build is the fallback.

A machine is claimed to be a vendor-built system or it is not, and "not" is a custom build. The
third value covered a machine whose firmware named neither a system nor a board maker - a real
situation, but not a third *kind*, and what it protected against (creating a listing with no usable
identity) is refused by ``create_listings_from_run`` on its own terms.

Existing rows are converted rather than left holding a value the field no longer offers.
"""
from django.db import migrations, models


def convert(apps, schema_editor):
    # Not a guess: re-deriving these through the new classifier gives "custom" too, because the
    # branch that used to return "unknown" now falls through to it. This is the same answer the
    # reader would produce today.
    apps.get_model("results", "TestRun").objects.filter(
        system_kind="unknown"
    ).update(system_kind="custom")
    # An alias is a reviewer *overriding* detection. "Unknown" as an override says "I cannot
    # tell", which under two kinds is the fallback - and the fallback is what detection returns
    # anyway, so the honest conversion is to no override at all rather than to a claim the
    # reviewer never made.
    apps.get_model("results", "ReportedIdentityAlias").objects.filter(
        resolved_kind="unknown"
    ).update(resolved_kind="")


def unconvert(apps, schema_editor):
    # Deliberately a no-op. Which rows said "unknown" is not recoverable, and inventing a set
    # to restore would be worse than leaving them as the classifier reads them today.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("results", "0002_testrun_available_from_minor"),
    ]

    operations = [
        migrations.RunPython(convert, unconvert),
        migrations.AlterField(
            model_name="reportedidentityalias",
            name="resolved_kind",
            field=models.CharField(
                blank=True,
                choices=[("prebuilt", "Prebuilt"), ("custom", "Custom build")],
                help_text="What this machine actually is, overriding what the firmware led the collector to guess.",
                max_length=10,
            ),
        ),
        migrations.AlterField(
            model_name="testrun",
            name="system_kind",
            field=models.CharField(
                choices=[("prebuilt", "Prebuilt"), ("custom", "Custom build")],
                db_index=True,
                default="custom",
                max_length=10,
            ),
        ),
    ]
