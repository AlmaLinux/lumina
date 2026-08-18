"""Vendor and the PCI ids stop being fields of their own and become ordinary conditions.

They were three inputs among the hundreds of paths a rule can read, and being fields made them
look like the only three - each fixed to one operator, so a rule could ask whether a vendor
*contained* a string but never whether it *was* one. Carrying the values across first, because a
rule somebody already saved has its whole meaning in those columns and dropping them silently
would widen it to every part of its kind.

The operators reproduce what the columns did: vendor was a case-insensitive substring, the ids
were exact.
"""
from django.db import migrations, models


def to_conditions(apps, schema_editor):
    model = apps.get_model("hardware", "ComponentNamingRule")
    for rule in model.objects.exclude(vendor="", vendor_id="", device_id=""):
        carried = [
            {"field": field, "op": op, "value": value}
            for field, op, value in (
                ("vendor", "icontains", rule.vendor),
                ("pci.vendor_id", "eq", rule.vendor_id),
                ("pci.device_id", "eq", rule.device_id),
            )
            if value
        ]
        existing = list(rule.conditions or [])
        # Appended, not prepended: conditions are all required, so order is cosmetic, and a
        # reviewer rereading the rule should find what they wrote where they wrote it.
        rule.conditions = existing + [c for c in carried if c not in existing]
        rule.save(update_fields=["conditions"])


def to_columns(apps, schema_editor):
    """Back out by reading the conditions the forward pass wrote, leaving the rest alone."""
    model = apps.get_model("hardware", "ComponentNamingRule")
    columns = {"vendor": "vendor", "pci.vendor_id": "vendor_id", "pci.device_id": "device_id"}
    for rule in model.objects.all():
        kept, changed = [], False
        for clause in rule.conditions or []:
            column = columns.get(clause.get("field"))
            if column and clause.get("op") in ("icontains", "eq"):
                setattr(rule, column, clause.get("value") or "")
                changed = True
            else:
                kept.append(clause)
        if changed:
            rule.conditions = kept
            rule.save(update_fields=["conditions", "vendor", "vendor_id", "device_id"])


class Migration(migrations.Migration):

    dependencies = [
        ("hardware", "0010_component_named_by"),
    ]

    operations = [
        migrations.RunPython(to_conditions, to_columns),
        migrations.RemoveField(
            model_name="componentnamingrule",
            name="device_id",
        ),
        migrations.RemoveField(
            model_name="componentnamingrule",
            name="vendor",
        ),
        migrations.RemoveField(
            model_name="componentnamingrule",
            name="vendor_id",
        ),
        migrations.AlterField(
            model_name="componentnamingrule",
            name="conditions",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text='Everything else the part must satisfy, all of it: [{"field": "pci.vendor_id", "op": "eq", "value": "8086"}]. Any field the template can read can be tested, with any operator, which is why vendor and the PCI ids are not fields here: they were three of the available hundreds, and being fields made them look like the only three.',
            ),
        ),
        migrations.AlterField(
            model_name="componentnamingrule",
            name="model_pattern",
            field=models.CharField(
                blank=True,
                help_text="Regular expression the reported model must match, case-insensitive (e.g. ^Altra). Its captures are what the template reads as match.1, match.2, which is why this is a field of its own rather than a condition.",
                max_length=200,
            ),
        ),
    ]
