"""Seed the known generic product lines.

Supermicro ships a whole range under the Product Name "Super Server"; the real model is on the
motherboard. Marking it here makes the catalog identify such machines by their board instead of
conflating every Supermicro under one listing. Admins can add more via the GenericModel admin.
"""
from django.db import migrations

SEED = [
    ("Supermicro", "Super Server",
     "Supermicro's generic product line; the real model is the motherboard."),
]


def seed(apps, schema_editor):
    GenericModel = apps.get_model("results", "GenericModel")
    for vendor, product, note in SEED:
        GenericModel.objects.get_or_create(
            vendor=vendor, product=product, defaults={"note": note},
        )


def unseed(apps, schema_editor):
    GenericModel = apps.get_model("results", "GenericModel")
    for vendor, product, _ in SEED:
        GenericModel.objects.filter(vendor=vendor, product=product).delete()


class Migration(migrations.Migration):

    dependencies = [("results", "0010_genericmodel")]

    operations = [migrations.RunPython(seed, unseed)]
