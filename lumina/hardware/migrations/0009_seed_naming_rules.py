"""The naming judgements that used to be constants in code, as rules anybody can read.

Both were written for a machine somebody had in front of them. Seeded rather than left in
``component_match`` so there is one mechanism and one place to look: a reviewer wondering why a
card is called what it is finds a row, not a regex in a module. They are marked ``built_in``,
which the admin uses to warn before an edit - changing one changes how every machine of that shape
is named.

The code they came from is still there and still runs, for a deployment with no rules and for a
part no rule matches. These reproduce it exactly, so the two cannot disagree.
"""
from django.db import migrations

RULES = [
    {
        "kind": "gpu",
        "priority": 10,
        "conditions": [
            {"field": "vendor", "op": "icontains", "value": "amd"},
            {"field": "model_is_marketing", "op": "eq", "value": False},
            {"field": "cpu.igpu_name", "op": "exists", "value": True},
            {"field": "machine.unnamed_amd_gpus", "op": "eq", "value": 1},
        ],
        "build": "{{ cpu.igpu_name }}",
        "notes": ("An AMD APU's brand string carries its iGPU's product name; pci.ids has only "
                  "the die codename. Only when exactly one AMD card is otherwise unnamed, since "
                  "the CPU names one iGPU and nothing says which card it is otherwise."),
    },
    {
        "kind": "gpu",
        "priority": 20,
        "conditions": [
            {"field": "model_is_generic", "op": "eq", "value": True},
            {"field": "cpu.model_without_igpu", "op": "exists", "value": True},
        ],
        "build": "{{ cpu.model_without_igpu }} ({{ model }})",
        "notes": ("pci.ids calls every recent Intel integrated GPU \"Intel Graphics\", so one "
                  "census bucket covered parts years apart. An integrated GPU has no identity "
                  "apart from the chip it is on, so it borrows one."),
    },
]


def seed(apps, schema_editor):
    model = apps.get_model("hardware", "ComponentNamingRule")
    for rule in RULES:
        model.objects.get_or_create(
            kind=rule["kind"], priority=rule["priority"],
            defaults={**rule, "built_in": True, "enabled": True, "fallback": ""},
        )


def unseed(apps, schema_editor):
    apps.get_model("hardware", "ComponentNamingRule").objects.filter(built_in=True).delete()


class Migration(migrations.Migration):

    dependencies = [("hardware", "0008_componentnamingrule")]

    operations = [migrations.RunPython(seed, unseed)]
