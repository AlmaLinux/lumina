"""The fields worth offering when a submitter says a part is misidentified.

Seven rows, one per place a real machine has been found hiding its identity. They are the
difference between asking somebody to read a payload and asking them a question about their own
hardware: the submitter sees values, and these decide which values.

Seeded rather than hardcoded for the usual reason - the next machine that hides its model
somewhere new should be a row somebody adds, not a release.
"""
from django.db import migrations

FIELDS = [
    ("cpu", "dmi.processor.*.Part Number", "Processor part number", 10),
    ("cpu", "dmi.processor.*.Version", "Processor version", 20),
    # Off by default: it is a number on most machines ("6"), and a candidate list is only useful
    # while it is short. Left as a row so turning it on is a tick rather than a migration.
    ("cpu", "dmi.processor.*.Family", "Processor family", 30),
    # Some boards put the model here and the revision in Product Name, or leave Product Name as a
    # family. It is also the only row that can ever contribute anything: a board is *named* by its
    # baseboard Product Name, so that row always matches what the part is already called and
    # filters itself out. It does carry a bare revision on plenty of machines ("A01", "ES1"), and
    # the label beside the value is what tells a reader that is what they are looking at.
    ("motherboard", "dmi.baseboard.*.Version", "Board version", 10),
    ("motherboard", "dmi.system.*.Product Name", "System product name", 20),
    # The whitebox case: an integrator who filled in nothing else sometimes names the board here.
    ("motherboard", "dmi.system.*.Version", "System version", 30),
    # Last, and only useful where the board is named something else entirely - it is what the part
    # is already called on nearly every machine, so it filters itself out.
    ("motherboard", "dmi.baseboard.*.Product Name", "Board product name", 40),
    # An NVIDIA card knows its own marketing name, and lspci only knows the die.
    ("gpu", "smi_name", "What nvidia-smi calls it", 10),
    ("gpu", "pci.subsystem_device", "Board the chip is on", 20),
]
OFF = {("cpu", "dmi.processor.*.Family")}


def seed(apps, schema_editor):
    model = apps.get_model("hardware", "NameSuggestionField")
    for kind, path, label, priority in FIELDS:
        model.objects.get_or_create(kind=kind, path=path, defaults={
            "label": label, "priority": priority, "built_in": True,
            "enabled": (kind, path) not in OFF,
        })


def unseed(apps, schema_editor):
    apps.get_model("hardware", "NameSuggestionField").objects.filter(built_in=True).delete()


class Migration(migrations.Migration):

    dependencies = [("hardware", "0014_name_suggestion_field")]

    operations = [migrations.RunPython(seed, unseed)]
