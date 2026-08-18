"""Undo any conflation the old generic-string matching already created.

Before generic product lines were recognized, a run reporting "Super Server" auto-linked at ingest
to whatever System the first such run produced, pooling different machines under one listing. This
unlinks the **not-yet-published** runs from any System whose identity is now a known generic line,
so each re-resolves to its own motherboard when next submitted - no re-run needed. Already-published
(attested) links are left alone: splitting a listing that carries certifications is a deliberate
de-conflation, not something to do silently on deploy.
"""
from django.db import migrations


def unlink(apps, schema_editor):
    TestRun = apps.get_model("results", "TestRun")
    System = apps.get_model("hardware", "System")
    GenericModel = apps.get_model("results", "GenericModel")

    generics = {
        ((g.vendor or "").strip().lower(), (g.product or "").strip().lower())
        for g in GenericModel.objects.all()
    }
    if not generics:
        return

    def is_generic(vendor_name, name):
        n = (name or "").strip().lower()
        v = (vendor_name or "").strip().lower()
        return ("", n) in generics or (v, n) in generics

    for system in System.objects.select_related("vendor"):
        vendor_name = system.vendor.name if system.vendor_id else ""
        if is_generic(vendor_name, system.name):
            TestRun.objects.filter(
                listing_system=system, published_at__isnull=True
            ).update(listing_system=None)


def noop(apps, schema_editor):
    # Not reversible: the correct re-link is re-derived from each run's board on its next submit,
    # so there is nothing to restore.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("results", "0011_seed_generic_models"),
        ("hardware", "0006_componentexclusionrule"),
    ]

    operations = [migrations.RunPython(unlink, noop)]
