r"""The iGPU-out-of-a-CPU-name judgement moves from code into the rules that use it.

``cpu.igpu_name`` and ``cpu.model_without_igpu`` were context fields computed by a module-level
pattern in ``component_match``: one anticipated AMD phrasing, ``w/ Radeon ...``, and a vendor who
spelled it any other way could not be named without a release. That is the thing this table exists
to replace, so the pattern moves into the two rules that read it, as ``match_field`` plus
``model_pattern`` with its captures, and the fields go.

The rules produce exactly what the code produced, so no part changes name. ``cpu.model_normalized``
takes over the half that was never about graphics: marks, clock speed, and core count off, which is
how the catalog spells every CPU.
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

BUILT_IN = {
    10: {
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
    20: {
        "match_field": "cpu.model_normalized",
        "model_pattern": CPU_WITHOUT_IGPU,
        "conditions": [{"field": "model_is_generic", "op": "eq", "value": True}],
        "build": "{{ match.1 }} ({{ model }})",
        "notes": ("pci.ids calls every recent Intel integrated GPU \"Intel Graphics\", so one "
                  "census bucket covered parts years apart. An integrated GPU has no identity "
                  "apart from the chip it is on, so it borrows one."),
    },
}

# Somebody else's rule reading a field that no longer exists. Pointed at the nearest thing that
# does, rather than left to render empty and rename their parts to something shorter in silence.
FIELD_MOVES = {"cpu.model_without_igpu": "cpu.model_normalized", "cpu.igpu_name": "cpu.model"}


def to_rules(apps, schema_editor):
    model = apps.get_model("hardware", "ComponentNamingRule")
    for rule in model.objects.all():
        fields = BUILT_IN.get(rule.priority) if rule.built_in else None
        if fields:
            for name, value in fields.items():
                setattr(rule, name, value)
            rule.save(update_fields=list(fields))
            continue
        rule.build = _moved(rule.build)
        rule.fallback = _moved(rule.fallback)
        rule.conditions = [
            {**clause, "field": FIELD_MOVES.get(clause.get("field"), clause.get("field"))}
            for clause in (rule.conditions or [])
        ]
        rule.save(update_fields=["build", "fallback", "conditions"])


def _moved(template: str) -> str:
    for old, new in FIELD_MOVES.items():
        template = (template or "").replace(old, new)
    return template


def back(apps, schema_editor):
    """Point the rules back at the fields, for a rollback to code that still computes them."""
    model = apps.get_model("hardware", "ComponentNamingRule")
    reverse = {new: old for old, new in FIELD_MOVES.items()}
    for rule in model.objects.all():
        if rule.built_in and rule.priority in BUILT_IN:
            rule.match_field = ""
            rule.model_pattern = ""
            rule.build = ("{{ cpu.igpu_name }}" if rule.priority == 10
                          else "{{ cpu.model_without_igpu }} ({{ model }})")
            if rule.priority == 10:
                rule.conditions = [*rule.conditions,
                                   {"field": "cpu.igpu_name", "op": "exists", "value": True}]
            else:
                rule.conditions = [*rule.conditions,
                                   {"field": "cpu.model_without_igpu", "op": "exists",
                                    "value": True}]
            rule.save(update_fields=["match_field", "model_pattern", "build", "conditions"])
            continue
        for new, old in reverse.items():
            rule.build = (rule.build or "").replace(new, old)
            rule.fallback = (rule.fallback or "").replace(new, old)
        rule.save(update_fields=["build", "fallback"])


class Migration(migrations.Migration):

    dependencies = [("hardware", "0012_naming_rule_match_field")]

    operations = [migrations.RunPython(to_rules, back)]
