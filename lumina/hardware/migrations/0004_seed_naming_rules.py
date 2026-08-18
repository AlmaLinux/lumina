"""The naming judgements that used to be constants in code, as rules anybody can read.

Both were written for a machine somebody had in front of them. Seeded rather than left in
``component_match`` so there is one mechanism and one place to look: a reviewer wondering why a
card is called what it is finds a row, not a regex in a module. They are marked ``built_in``,
which the admin uses to warn before an edit - changing one changes how every machine of that shape
is named.

There is no second naming behaviour beneath these. The code they came from is gone, so turning
one off means exactly what it says.
"""
from django.db import migrations

# The token after "Radeon" is required, which is what keeps a bare "Radeon Graphics" whole: with
# nothing else to go on, the generic string is the part's real name and trimming it to "Radeon"
# would name nothing. "w/ Radeon 780M Graphics" gives "Radeon 780M"; "with Radeon Graphics" gives
# "Radeon Graphics"; "w/ Radeon 890M" gives "Radeon 890M".
IGPU_IN_CPU = r"\bw(?:/|ith)\s+(Radeon\b(?:\s+RX)?\s+\S+.*?)(?:\s+(?:Graphics|Gfx))?$"
# The CPU's own name, with an iGPU the brand string advertises left off, so a GPU qualified by its
# CPU does not say the graphics part twice.
CPU_WITHOUT_IGPU = r"^(.+?)(?:\s+w(?:/|ith)\s+Radeon\b.*)?$"

RULES = [
    {
        "kind": "gpu",
        "priority": 10,
        "match_field": "cpu.model",
        "model_pattern": IGPU_IN_CPU,
        "conditions": [
            {"field": "vendor", "op": "icontains", "value": "amd"},
            {"field": "model_is_marketing", "op": "eq", "value": False},
            {"field": "machine.unnamed_amd_gpus", "op": "eq", "value": 1},
        ],
        "build": "{{ match.1 }}",
        "notes": ("An AMD APU's brand string carries its iGPU's product name; pci.ids has only "
                  "the die codename. Only when exactly one AMD card is otherwise unnamed, since "
                  "the CPU names one iGPU and nothing says which card it is otherwise."),
    },
    {
        "kind": "gpu",
        "priority": 20,
        "match_field": "cpu.model_normalized",
        "model_pattern": CPU_WITHOUT_IGPU,
        "conditions": [{"field": "model_is_generic", "op": "eq", "value": True}],
        "build": "{{ match.1 }} ({{ model }})",
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

    dependencies = [("hardware", "0003_reference_data")]

    operations = [migrations.RunPython(seed, unseed)]
