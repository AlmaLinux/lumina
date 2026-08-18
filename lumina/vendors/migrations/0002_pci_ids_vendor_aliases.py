"""Alias the pci.ids spellings of the silicon vendors.

The table and the reasoning live in ``lumina.vendors.pci_aliases``, imported rather than copied.
That is a deliberate departure from freezing a data migration's inputs: this one is additive and
idempotent, and its purpose is "make sure these aliases exist" rather than "reproduce the state of
that table on the day it was written". A second copy here would be the one that goes stale.
"""
from django.db import migrations

from lumina.vendors.pci_aliases import PCI_VENDOR_ALIASES, ensure


def add(apps, schema_editor):
    ensure(apps.get_model("vendors", "Vendor"), apps.get_model("vendors", "VendorAlias"))


def remove(apps, schema_editor):
    apps.get_model("vendors", "VendorAlias").objects.filter(
        name__in=[spelling for spelling, _ in PCI_VENDOR_ALIASES]
    ).delete()


class Migration(migrations.Migration):

    dependencies = [("vendors", "0001_initial")]

    operations = [migrations.RunPython(add, remove)]
