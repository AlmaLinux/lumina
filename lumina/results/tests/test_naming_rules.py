"""Naming is a table an admin owns, not constants in a module.

Each hardcoded rule was written for a machine somebody had in front of them, and the next
unidentifiable part meant a release: an Ampere Altra carries its model in the DMI processor
structure's Part Number, and there was no way to say so. A rule is a match and a template, and the
template reads an allowlisted view of the run.
"""
from __future__ import annotations

import pytest

from lumina.hardware.models import ComponentNamingRule, NameSuggestionField
from lumina.results import naming
from lumina.results.component_match import gpu_display_name

pytestmark = pytest.mark.django_db

AMPERE = {"summary": {
    "cpus": [{"model": "Ampere(R) Altra(R) Processor", "cores": 80, "vendor": "ARM"}],
    "fields": {"dmi": {"processor": [{"props": {"Part Number": "Q80-30", "Core Count": "80"}}]}},
}}


def _cpu_context(payload=None, **overrides):
    context = naming.build_context("cpu", {}, payload or AMPERE)
    return context | {"vendor": "Ampere", "model": "Ampere Altra Processor", **overrides}


def _rule(**kw):
    kw.setdefault("build", "{{ model }}")
    kw.setdefault("priority", 50)
    return ComponentNamingRule.objects.create(**kw)


# --- the case that prompted this --------------------------------------------------


def test_a_rule_can_name_a_part_from_a_field_nobody_promoted():
    """The Altra's model is in DMI, which no summary key carries. Before rules, identifying it
    meant a release of the collecting suite."""
    _rule(kind="cpu", conditions=[{"field": "vendor", "op": "icontains", "value": "Ampere"}],
          build="Ampere Altra {{ dmi.processor[0]['Part Number'] }}")

    assert naming.apply_rules(_cpu_context())[0] == "Ampere Altra Q80-30"


def test_the_fallback_runs_when_the_machine_did_not_report_that_field():
    """An older bundle has no ``fields`` at all. Composing something unique from core count beats
    collapsing every Altra into one bucket, which is what a blank name would do."""
    _rule(kind="cpu", conditions=[{"field": "vendor", "op": "icontains", "value": "Ampere"}],
          build="Ampere Altra {{ dmi.processor[0]['Part Number'] }}",
          fallback="{{ model }} ({{ cpu.cores }}-core)")
    payload = {"summary": {"cpus": AMPERE["summary"]["cpus"]}}

    assert naming.apply_rules(_cpu_context(payload))[0] == "Ampere Altra Processor (80-core)"


def test_a_rule_that_produces_nothing_lets_the_next_one_try():
    """A specific rule that does not apply must not mask a general one that would."""
    _rule(kind="cpu", priority=10, build="{{ dmi.nothing[0]['Nope'] }}")
    _rule(kind="cpu", priority=20, build="general answer")

    name, rule = naming.apply_rules(_cpu_context())

    assert name == "general answer"
    assert rule.priority == 20


def test_no_rule_matching_names_nothing_rather_than_guessing():
    _rule(kind="gpu", build="not for a cpu")

    assert naming.apply_rules(_cpu_context()) == ("", None)


# --- what a rule may match on -----------------------------------------------------


def test_priority_decides_which_of_two_matching_rules_wins():
    _rule(kind="cpu", priority=20, build="second")
    _rule(kind="cpu", priority=10, build="first")

    assert naming.apply_rules(_cpu_context())[0] == "first"


def test_a_pattern_capture_is_available_to_the_template():
    _rule(kind="cpu", model_pattern=r"Altra (\w+)", build="Altra {{ match['1'] }}")

    assert naming.apply_rules(_cpu_context())[0] == "Altra Processor"


def test_a_condition_on_a_nested_field_is_honoured():
    _rule(kind="cpu", build="80-core part",
          conditions=[{"field": "dmi.processor.0.Core Count", "op": "eq", "value": "80"}])

    assert naming.apply_rules(_cpu_context())[0] == "80-core part"


def test_a_condition_that_does_not_hold_stops_the_rule():
    _rule(kind="cpu", build="never",
          conditions=[{"field": "cpu.cores", "op": "gt", "value": 200}])

    assert naming.apply_rules(_cpu_context()) == ("", None)


def test_the_pci_ids_narrow_a_rule_to_one_part():
    gpu = {"driver": "nvidia",
           "pci_ids": {"vendor": "NVIDIA Corporation [10de]", "device": "AD102 [26b9]"}}
    _rule(kind="gpu", build="the exact card", conditions=[
        {"field": "pci.vendor_id", "op": "eq", "value": "10de"},
        {"field": "pci.device_id", "op": "eq", "value": "26b9"},
    ])
    context = naming.build_context("gpu", gpu, {"summary": {}}, [gpu])

    assert naming.apply_rules(context)[0] == "the exact card"


# --- the sandbox ------------------------------------------------------------------


def test_a_template_cannot_reach_outside_what_it_was_given():
    """A reviewer authoring a rule is running something server-side. The sandbox is what makes it
    a formatting language rather than an execution one."""
    _rule(kind="cpu", build="{{ ''.__class__.__mro__ }}")

    assert naming.apply_rules(_cpu_context()) == ("", None)


def test_a_template_that_throws_does_not_take_the_run_with_it():
    """A rule is configuration, and bad configuration must not stop a run resolving."""
    _rule(kind="cpu", priority=10, build="{{ 1 / 0 }}")
    _rule(kind="cpu", priority=20, build="still resolved")

    assert naming.apply_rules(_cpu_context())[0] == "still resolved"


def test_the_context_carries_no_submitter_or_request():
    """An allowlist, not the payload: what a template can read is a decision."""
    context = naming.build_context("cpu", {}, AMPERE)

    assert set(context) == {
        "kind", "vendor", "model", "raw_model", "model_is_generic", "model_is_marketing",
        "pci", "cpu", "dmi", "fields", "machine",
    }


def test_every_namespace_the_collector_recorded_is_readable():
    """The suite writes ``summary.fields`` as a namespace map so a rule can read a field nobody
    thought worth promoting. Lumina reached in for ``dmi`` by name and dropped the rest, so
    ``fields.pci`` was already unreadable - and a machine that reports its identity some other way
    could never be named at all."""
    payload = {"summary": {"fields": {
        "dmi": {"processor": [{"props": {"Part Number": "Q80-30"}}]},
        "devicetree": {"model": "Ampere Mt. Collins"},
    }}}

    context = naming.build_context("cpu", {}, payload)

    assert context["fields"]["devicetree"]["model"] == "Ampere Mt. Collins"


def test_a_new_namespace_needs_no_lumina_change():
    """The point of the change: a collector that learns to read a device tree is addressable by a
    rule the moment somebody types the path."""
    payload = {"summary": {"fields": {"devicetree": {"model": "Ampere Mt. Collins"}}}}
    _rule(kind="cpu", priority=1, match_field="fields.devicetree.model",
          model_pattern=r"^Ampere (.+)$", build="{{ match.1 }}")

    assert naming.apply_rules(naming.build_context("cpu", {}, payload))[0] == "Mt. Collins"


def test_the_flattened_dmi_view_still_reads_the_same():
    """Every rule written so far addresses ``dmi.processor.0.Part Number``, and the wrapper the
    collector puts around each structure is not something they should have to know about."""
    context = naming.build_context("cpu", {}, AMPERE)

    assert context["dmi"]["processor"][0]["Part Number"] == "Q80-30"
    assert context["fields"]["dmi"]["processor"][0]["props"]["Part Number"] == "Q80-30"


# --- saving a rule ----------------------------------------------------------------


def test_a_broken_regex_is_refused_when_it_is_typed():
    with pytest.raises(Exception, match="regular expression"):
        ComponentNamingRule(kind="cpu", model_pattern="(unclosed", build="x").clean()


def test_a_broken_template_is_refused_when_it_is_typed():
    with pytest.raises(Exception, match="build"):
        ComponentNamingRule(kind="cpu", build="{{ unclosed ").clean()


def test_an_unknown_condition_operator_is_refused():
    with pytest.raises(Exception, match="operator"):
        ComponentNamingRule(
            kind="cpu", build="x",
            conditions=[{"field": "model", "op": "sounds_like", "value": "y"}]).clean()


def test_a_rule_with_no_template_names_nothing_and_is_refused():
    with pytest.raises(Exception, match="names nothing"):
        ComponentNamingRule(kind="cpu", build="  ").clean()


def test_two_rules_that_cannot_be_told_apart_are_refused():
    """An ambiguous pair is a bug being written down, so it is refused rather than resolved by
    an arbitrary tie-break nobody can see."""
    same = [{"field": "vendor", "op": "icontains", "value": "Ampere"}]
    _rule(kind="cpu", conditions=same, priority=30, build="one")

    with pytest.raises(Exception, match="priority"):
        ComponentNamingRule(kind="cpu", conditions=same, priority=30, build="two").clean()


def test_the_same_match_at_a_different_priority_is_allowed():
    """Which is how a rule is superseded: write the new one above the old."""
    same = [{"field": "vendor", "op": "icontains", "value": "Ampere"}]
    _rule(kind="cpu", conditions=same, priority=30, build="one")

    ComponentNamingRule(kind="cpu", conditions=same, priority=20, build="two").clean()


# --- the built-ins, as rows -------------------------------------------------------


@pytest.mark.parametrize("cpu,device,vendor,expected", [
    ("AMD Ryzen 7 PRO 8840HS w/ Radeon 780M Graphics", "HawkPoint1 [150e]",
     "Advanced Micro Devices, Inc. [AMD/ATI] [1002]", "Radeon 780M"),
    ("Intel(R) Core(TM) Ultra 9 275HX", "Arrow Lake-S [Intel Graphics]",
     "Intel Corporation [8086]", "Intel Core Ultra 9 275HX (Intel Graphics)"),
    ("Intel(R) Xeon(R) Gold 6430", "AD102 [GeForce RTX 4090]",
     "NVIDIA Corporation [10de]", "GeForce RTX 4090"),
])
def test_the_seeded_rules_reproduce_what_the_code_did(cpu, device, vendor, expected):
    """They are the same judgements, moved from constants into rows. If these drift, a machine is
    named one way by the table and another by the fallback beneath it."""
    gpu = {"driver": "x", "pci_ids": {"vendor": vendor, "device": device}}

    assert gpu_display_name(gpu, cpu, [gpu]) == expected


def test_the_built_ins_are_marked_so_an_edit_can_warn():
    assert ComponentNamingRule.objects.filter(built_in=True).count() == 2


def test_a_disabled_rule_does_not_fire():
    """The switch an admin reaches for when a rule turns out wrong: leave it in place, turn it
    off, and the behaviour beneath it comes back."""
    gpu = {"driver": "x", "pci_ids": {"vendor": "Intel Corporation [8086]",
                                      "device": "Arrow Lake-S [Intel Graphics]"}}
    _rule(kind="gpu", priority=1, build="never", enabled=False,
          conditions=[{"field": "pci.vendor_id", "op": "eq", "value": "8086"}])

    assert gpu_display_name(gpu, "Intel(R) Core(TM) Ultra 9 275HX", [gpu]) == (
        "Intel Core Ultra 9 275HX (Intel Graphics)")


def test_a_rule_beats_the_built_in_behaviour():
    """The whole point: a correction is a row, not a release."""
    gpu = {"driver": "x", "pci_ids": {"vendor": "Intel Corporation [8086]",
                                      "device": "Arrow Lake-S [Intel Graphics]"}}
    _rule(kind="gpu", priority=1, build="Arc-era iGPU",
          conditions=[{"field": "pci.vendor_id", "op": "eq", "value": "8086"}])

    assert gpu_display_name(gpu, "Intel(R) Core(TM) Ultra 9 275HX", [gpu]) == "Arc-era iGPU"


# --- what the builder offers a reviewer --------------------------------------------
#
# A rule can read hundreds of paths. Three of them used to be form fields, which made those three
# look like the whole vocabulary, so the rest of it has to be discoverable from the form.


def test_every_readable_path_is_offered_with_its_value():
    """The values are the point. A reviewer looking at one part decides what about it is
    distinctive, and a list of bare names makes them guess which path holds "Q80-30"."""
    fields = {f["path"]: f["value"] for f in naming.available_fields(_cpu_context())}

    assert fields["vendor"] == "Ampere"
    assert fields["cpu.cores"] == "80"
    assert fields["kind"] == "cpu"


def test_a_list_is_offered_by_index_not_as_a_whole():
    """``dmi.processor.0.Part Number`` is the path a condition and a template both use. Stopping
    at ``dmi.processor`` would leave the reviewer to work out the rest of it."""
    fields = {f["path"]: f["value"] for f in naming.available_fields(_cpu_context())}

    assert fields["dmi.processor.0.Part Number"] == "Q80-30"


def test_a_missing_value_is_offered_as_empty_rather_than_the_word_none():
    """It goes straight into a form field, and "None" there is a condition that matches nothing."""
    fields = {f["path"]: f["value"] for f in naming.available_fields({"a": {"b": None}})}

    assert fields["a.b"] == ""


def test_the_paths_come_back_in_a_stable_order():
    """It is a suggestion list somebody scans, and dict order is the order the context happened to
    be built in."""
    paths = [f["path"] for f in naming.available_fields(_cpu_context())]

    assert paths == sorted(paths)


def test_the_suggested_conditions_identify_the_part():
    """Prefilled rather than blank because the common case is tweaking what was detected."""
    context = naming.build_context("gpu", {
        "pci_ids": {"vendor": "Intel Corporation [8086]", "device": "Arrow Lake-S [7d67]"}})

    assert naming.suggested_conditions(context) == [
        {"field": "pci.vendor_id", "op": "eq", "value": "8086"},
        {"field": "pci.device_id", "op": "eq", "value": "7d67"},
    ]


def test_a_suggested_condition_is_exact_not_a_substring():
    """A device id is four hex digits, and "7d67 is contained in" matches parts nobody meant."""
    context = naming.build_context("gpu", {
        "pci_ids": {"vendor": "Intel Corporation [8086]", "device": "Arrow Lake-S [7d67]"}})

    assert {c["op"] for c in naming.suggested_conditions(context)} == {"eq"}


def test_nothing_is_suggested_for_an_id_the_machine_did_not_report():
    """An empty condition tests nothing, so a rule carrying one is wider than its author meant."""
    context = naming.build_context("gpu", {"pci_ids": {"vendor": "Intel Corporation [8086]"}})

    assert naming.suggested_conditions(context) == [
        {"field": "pci.vendor_id", "op": "eq", "value": "8086"},
    ]


def test_a_rule_can_match_on_any_field_not_just_the_three_that_had_columns():
    """The request behind the change: a part identified by something other than vendor or a PCI
    id, here the DMI part number an Ampere Altra hides its model in."""
    _rule(kind="cpu", priority=1, build="Ampere Altra {{ dmi.processor.0['Part Number'] }}",
          conditions=[{"field": "dmi.processor.0.Part Number", "op": "eq", "value": "Q80-30"}])

    assert naming.apply_rules(_cpu_context()) == ("Ampere Altra Q80-30", naming.active_rules()[0])


def test_an_operator_other_than_contains_is_honoured():
    """"vendor is" and "vendor contains" are different rules, and only one of them was expressible."""
    _rule(kind="cpu", priority=1, build="exact",
          conditions=[{"field": "vendor", "op": "eq", "value": "Ampere"}])
    _rule(kind="cpu", priority=2, build="substring",
          conditions=[{"field": "vendor", "op": "icontains", "value": "amp"}])

    assert naming.apply_rules(_cpu_context(vendor="Amperex"))[0] == "substring"


# --- writing the path into a template ----------------------------------------------


def test_a_plain_path_is_written_as_a_dotted_name():
    assert naming.template_expression("cpu.model") == "{{ cpu.model }}"


def test_a_list_index_stays_a_dotted_name():
    """Jinja reads ``a.0`` as a subscript, and it is what the field list shows."""
    assert naming.template_expression("dmi.processor.0") == "{{ dmi.processor.0 }}"


def test_a_key_with_a_space_is_subscripted():
    """The whole reason the field list hands over an expression and not the path: this is the case
    a reviewer gets wrong, and the Ampere Altra rule is written on exactly this field."""
    assert naming.template_expression("dmi.processor.0.Part Number") == (
        '{{ dmi.processor.0["Part Number"] }}')


def test_a_key_with_a_quote_is_escaped_rather_than_ending_the_string():
    """dmidecode reports whatever the board's firmware put there."""
    rendered = naming.render(
        naming.template_expression('dmi.x.0.a"b'), {"dmi": {"x": [{'a"b': "ok"}]}})

    assert rendered == "ok"


def test_the_expression_a_field_hands_over_actually_renders():
    """The two halves have to agree: a suggestion that does not render is worse than none."""
    context = _cpu_context()
    fields = {f["path"]: f["expr"] for f in naming.available_fields(context)}

    assert naming.render(fields["dmi.processor.0.Part Number"], context) == "Q80-30"


# --- reading the field list --------------------------------------------------------


def test_fields_are_grouped_by_their_first_segment():
    groups = dict(naming.field_groups(naming.available_fields(_cpu_context())))

    assert {"pci.vendor_id", "pci.slot"} <= {f["path"] for f in groups["pci"]}
    assert [f["path"] for f in groups["dmi"]] == ["dmi.processor.0.Core Count",
                                                  "dmi.processor.0.Part Number"]


def test_the_part_s_own_fields_are_a_group_of_their_own_and_come_first():
    """``vendor`` and ``model`` have no prefix, and "everything else" is not a usable heading."""
    groups = naming.field_groups(naming.available_fields(_cpu_context()))

    assert groups[0][0] == "device"
    assert "vendor" in {f["path"] for f in groups[0][1]}


def test_the_remaining_groups_read_in_name_order():
    """It is a list somebody scans for a prefix they half remember, so the order is the list's own
    rather than whatever order the fields happened to arrive in."""
    groups = naming.field_groups([
        {"path": path, "value": "", "expr": ""}
        for path in ("pci.slot", "cpu.model", "vendor", "machine.gpus", "dmi.a.0.b")
    ])

    assert [group for group, _entries in groups] == ["device", "cpu", "dmi", "machine", "pci"]


def test_grouping_nothing_produces_no_groups():
    assert naming.field_groups([]) == []


# --- regex and its captures, on any field ------------------------------------------
#
# Pulling a part's name out of another field was hardcoded: an AMD iGPU was read out of a CPU
# brand string by a module-level pattern feeding two derived context fields, so the one phrasing
# somebody had anticipated was the only one that could work and widening it took a release.


def test_a_capture_is_read_by_number():
    """``match.1`` is what the field's own help text says to write. Jinja resolves that dotted
    digit as an integer subscript, and the captures were keyed by string alone, so every rule
    written the documented way rendered empty with nothing to say why."""
    _rule(kind="cpu", priority=1, match_field="cpu.model",
          model_pattern=r"\((\w+)\)", build="{{ match.1 }}")

    assert naming.apply_rules(_cpu_context())[0] == "R"


def test_a_capture_is_read_by_name():
    """A named group says what the capture is for, where a number says only where it sat."""
    _rule(kind="cpu", priority=1, match_field="dmi.processor.0.Part Number",
          model_pattern=r"^(?P<series>Q\d+)", build="Ampere Altra {{ match.series }}")

    assert naming.apply_rules(_cpu_context())[0] == "Ampere Altra Q80"


def test_the_whole_match_is_capture_zero():
    _rule(kind="cpu", priority=1, match_field="cpu.model", model_pattern=r"Altra",
          build="{{ match.0 }}")

    assert naming.apply_rules(_cpu_context())[0] == "Altra"


def test_the_pattern_runs_against_the_field_the_rule_names():
    """Not the part's model. Naming a GPU after something in the CPU string was the case that had
    to be hardcoded, because a pattern could only ever look at the model."""
    _rule(kind="cpu", priority=1, match_field="dmi.processor.0.Part Number",
          model_pattern=r"^(Q80-30)$", build="{{ match.1 }}")

    assert naming.apply_rules(_cpu_context())[0] == "Q80-30"


def test_an_unnamed_field_still_means_the_model():
    """Every rule written before the field existed, and the common case after it."""
    _rule(kind="cpu", priority=1, model_pattern=r"^(Ampere)", build="{{ match.1 }} silicon")

    assert naming.apply_rules(_cpu_context())[0] == "Ampere silicon"


def test_a_pattern_that_does_not_match_the_named_field_does_not_apply():
    _rule(kind="cpu", priority=1, match_field="cpu.vendor", model_pattern=r"^Intel",
          build="never")

    assert naming.apply_rules(_cpu_context())[0] == ""


def test_a_missing_field_is_not_a_match_and_not_a_crash():
    _rule(kind="cpu", priority=1, match_field="dmi.nothing.0.here", model_pattern=r".",
          build="never")

    assert naming.apply_rules(_cpu_context())[0] == ""


def test_two_rules_matching_different_fields_are_not_ambiguous():
    """``match_field`` is part of what a rule matches, so two rules reading different fields with
    the same pattern are two different rules and may share a priority."""

    _rule(kind="cpu", priority=1, match_field="cpu.model", model_pattern=r"x", build="a")
    second = ComponentNamingRule(kind="cpu", priority=1, match_field="cpu.vendor",
                                 model_pattern=r"x", build="b")

    second.full_clean()  # raises if the pair reads as ambiguous


def test_the_same_field_and_pattern_at_one_priority_is_still_refused():
    from django.core.exceptions import ValidationError

    _rule(kind="cpu", priority=1, match_field="cpu.model", model_pattern=r"x", build="a")
    second = ComponentNamingRule(kind="cpu", priority=1, match_field="cpu.model",
                                 model_pattern=r"x", build="b")

    with pytest.raises(ValidationError, match="already matches"):
        second.full_clean()


def test_a_cpu_qualifying_a_generic_gpu_does_not_say_graphics_twice():
    """An AMD brand string already carries the graphics part, so borrowing the whole string to
    qualify a generic GPU name repeats it: "Ryzen 5 5600G with Radeon Graphics (Radeon Graphics)".
    The seeded rule trims it, which is the half of the old ``cpu_without_igpu`` that was never
    about normalization."""
    gpu = {"driver": "amdgpu",
           "pci_ids": {"vendor": "Advanced Micro Devices, Inc. [AMD/ATI] [1002]",
                       "device": "Barcelo [Radeon Graphics] [15e7]"}}

    assert gpu_display_name(gpu, "AMD Ryzen 5 5600G with Radeon Graphics", [gpu]) == (
        "AMD Ryzen 5 5600G (Radeon Graphics)")


# --- the cheatsheet on the builder page --------------------------------------------
#
# Every example it prints, run through the engine. A reference with a wrong example in it is worse
# than no reference: somebody copies it, gets an empty name, and concludes the feature is broken.

CHEATSHEET_CPU = "AMD Ryzen 7 PRO 7840U w/ Radeon 780M Graphics"


@pytest.mark.parametrize("pattern,build,expected", [
    (r"w/ (Radeon \S+)", "{{ match.1 }}", "Radeon 780M"),
    (r"w/ (?P<igpu>Radeon \S+)", "{{ match.igpu }}", "Radeon 780M"),
    (r"(Radeon) (\S+)", "{{ match.2 }} by {{ match.1 }}", "780M by Radeon"),
    (r"w/ Radeon \S+", "{{ match.0 }}", "w/ Radeon 780M"),
])
def test_the_grouping_examples_do_what_the_cheatsheet_says(pattern, build, expected):
    _rule(kind="cpu", priority=1, match_field="cpu.model", model_pattern=pattern, build=build)
    context = naming.build_context("cpu", {}, {"summary": {"cpus": [{"model": CHEATSHEET_CPU}]}})

    assert naming.apply_rules(context)[0] == expected


@pytest.mark.parametrize("cpu_model", [
    "AMD Ryzen 7 PRO 7840U w/ Radeon 780M Graphics",
    "AMD Ryzen 5 2400G with Radeon Vega 11 Graphics",
])
def test_a_non_capturing_group_accepts_both_spellings(cpu_model):
    """The example the page gives for ``(?:...)``."""
    _rule(kind="cpu", priority=1, match_field="cpu.model",
          model_pattern=r"w(?:/|ith) (Radeon \S+)", build="{{ match.1 }}")
    context = naming.build_context("cpu", {}, {"summary": {"cpus": [{"model": cpu_model}]}})

    assert naming.apply_rules(context)[0].startswith("Radeon ")


def test_a_non_capturing_group_is_not_a_capture():
    """The whole point of the example: it groups without becoming ``match.1``."""
    _rule(kind="cpu", priority=1, match_field="cpu.model",
          model_pattern=r"w(?:/|ith)", build="[{{ match.1 }}]")
    context = naming.build_context("cpu", {}, {"summary": {"cpus": [{"model": CHEATSHEET_CPU}]}})

    assert naming.apply_rules(context)[0] == "[]"


def test_a_pattern_that_does_not_match_gates_the_rule_out():
    """The first thing the cheatsheet claims the pattern does."""
    _rule(kind="cpu", priority=1, match_field="cpu.model", model_pattern=r"Intel",
          build="never")
    context = naming.build_context("cpu", {}, {"summary": {"cpus": [{"model": CHEATSHEET_CPU}]}})

    assert naming.apply_rules(context)[0] == ""


def test_matching_ignores_case_as_the_page_says():
    _rule(kind="cpu", priority=1, match_field="cpu.model", model_pattern=r"(radeon \S+)",
          build="{{ match.1 }}")
    context = naming.build_context("cpu", {}, {"summary": {"cpus": [{"model": CHEATSHEET_CPU}]}})

    assert naming.apply_rules(context)[0] == "Radeon 780M"


# --- kinds that are two strings, not a device on a bus ------------------------------
#
# The engine only ever named GPUs. A CPU and a motherboard are reported as a vendor and a model
# string, have no PCI identity, and were named by whatever the firmware said - which for an Ampere
# Altra is "Ampere(R) Altra(R) Processor" with the actual model hidden in a DMI field.


def test_a_cpu_is_named_by_a_rule():
    """The case this started from: the model is in the DMI processor structure's Part Number and
    no amount of reading the brand string finds it."""
    from lumina.results.component_match import name_status

    _rule(kind="cpu", priority=1, match_field="dmi.processor.0.Part Number",
          model_pattern=r"^(Q\d+-\d+)$", build="Ampere Altra {{ match.1 }}")

    status = name_status("cpu", "Ampere", "Ampere(R) Altra(R) Processor", AMPERE)

    assert status["name"] == "Ampere Altra Q80-30"
    assert status["rule"] is not None


def test_a_motherboard_is_named_by_a_rule():
    from lumina.results.component_match import name_status

    _rule(kind="motherboard", priority=1, model_pattern=r"^(\w+)-",
          build="{{ match.1 }} series board")

    status = name_status("motherboard", "Supermicro", "X11DPi-N")

    assert status["name"] == "X11DPi series board"


def test_a_cpu_rule_sees_the_part_as_model_and_vendor():
    """The context a rule reads has to hold the part in front of it, not a GPU's idea of one."""
    from lumina.results.component_match import name_status

    _rule(kind="cpu", priority=1, build="{{ vendor }} / {{ model }}")

    assert name_status("cpu", "Ampere", "Altra Q80-30")["name"] == "Ampere / Altra Q80-30"


def test_a_gpu_rule_does_not_fire_on_a_cpu():
    """Kind is part of the match, and a rule written about graphics naming a processor would be a
    quiet disaster across every machine."""
    from lumina.results.component_match import name_status

    _rule(kind="gpu", priority=1, build="never")

    assert name_status("cpu", "Ampere", "Altra Q80-30")["name"] == "Altra Q80-30"


def test_a_kindless_rule_reaches_every_kind():
    from lumina.results.component_match import name_status

    _rule(kind="", priority=1, model_pattern=r"Altra", build="any kind")

    assert name_status("cpu", "Ampere", "Altra Q80-30")["name"] == "any kind"


def test_an_unmatched_cpu_keeps_what_the_firmware_said():
    from lumina.results.component_match import name_status

    status = name_status("cpu", "Ampere", "Ampere(R) Altra(R) Processor")

    assert status == {"reported": "Ampere(R) Altra(R) Processor",
                      "name": "Ampere(R) Altra(R) Processor", "rule": None,
                      "changed": False, "unidentified": False}


def test_cpu_and_motherboard_are_kinds_a_rule_reaches():
    """The form marks kinds nothing reads, and this is what it reads from."""
    assert set(naming.RULED_KINDS) >= {"gpu", "cpu", "motherboard"}


def test_a_board_name_is_not_rewritten_by_gpu_conventions():
    """A GPU's name is read out of pci.ids, where brackets mean the product inside a codename.
    A board is a DMI string and means exactly what it says, brackets and all."""
    from lumina.results.component_match import name_status

    assert name_status("motherboard", "Supermicro", "X11DPi-N [Rev 1.02]")["name"] == (
        "X11DPi-N [Rev 1.02]")


def test_a_rule_for_a_cpu_sees_the_string_the_firmware_reported():
    """Not a laundered one. Reading a card's name rejects pci.ids placeholders - "Device" means
    "this table has no name for it" - and a CPU whose firmware reports that same word is telling
    the truth. Run through the card reader, its model reaches a rule as nothing at all."""
    from lumina.results.component_match import name_status

    _rule(kind="cpu", priority=1, model_pattern=r"^(Device)$", build="Named {{ match.1 }}")

    assert name_status("cpu", "Unknown", "Device")["name"] == "Named Device"


# --- what a submitter is offered when they say a part is wrong ----------------------
#
# Values, never paths: being asked which field holds the real model is being asked to understand
# the payload. The list has to be short enough to read, or the disclosure hides a mess rather than
# a question.

ALTRA_DMI = {"summary": {"fields": {"dmi": {
    "processor": [{"props": {
        "Manufacturer": "Ampere(R)", "Version": "Ampere(R) Altra(R) Processor",
        "Part Number": "Q80-30", "Core Count": "80", "Serial Number": "Not Specified",
        "Socket Designation": "CPU0", "Max Speed": "3000 MHz",
    }}],
    "baseboard": [{"props": {
        "Manufacturer": "FOXCONN", "Product Name": "Mt. Collins", "Version": "ES1",
    }}],
}}}}


def _context(kind="cpu", vendor="Arm", model="Neoverse-N1", payload=None):
    return naming.build_context(kind, {"vendor": vendor, "model": model}, payload or ALTRA_DMI)


def _values(kind="cpu", current="Neoverse-N1", **kw):
    return [c["value"] for c in naming.candidates(_context(kind=kind, **kw), kind, current)]


def test_the_machine_s_own_values_are_offered():
    """The case this exists for: an Altra's model is in the DMI part number and no amount of
    reading the brand string finds it."""
    assert _values() == ["Q80-30", "Ampere(R) Altra(R) Processor"]


def test_only_the_paths_somebody_configured_are_offered():
    """Not every field the machine reported. The Altra's processor structure has seven here and
    dozens in life, and a list of dozens is a list nobody reads."""
    offered = _values()

    assert "80" not in offered and "CPU0" not in offered and "3000 MHz" not in offered


def test_a_path_can_be_turned_off_without_a_release():
    NameSuggestionField.objects.filter(path="dmi.processor.*.Part Number").update(enabled=False)

    assert _values() == ["Ampere(R) Altra(R) Processor"]


def test_a_new_path_is_a_row_somebody_adds():
    """The next machine that hides its model somewhere new."""
    NameSuggestionField.objects.create(
        kind="cpu", path="dmi.processor.*.Socket Designation", label="Socket", priority=5)

    assert "CPU0" in _values()


def test_the_order_is_the_order_the_rows_give():
    """It is a list somebody reads top down, and the likeliest answer belongs first."""
    NameSuggestionField.objects.filter(path="dmi.processor.*.Version").update(priority=1)

    assert _values() == ["Ampere(R) Altra(R) Processor", "Q80-30"]


def test_what_the_part_is_already_called_is_not_offered():
    """Offering the answer already on screen as a correction to itself."""
    assert "Ampere(R) Altra(R) Processor" not in _values(
        current="Ampere(R) Altra(R) Processor")


def test_firmware_placeholders_are_not_offered():
    """"Not Specified" as a suggested model is exactly the confusing option a short list exists to
    avoid. The set is ``inventory_extract``'s, not a second one."""
    payload = {"summary": {"fields": {"dmi": {"processor": [{"props": {
        "Part Number": "Not Specified", "Version": "To be filled by O.E.M."}}]}}}}

    assert _values(payload=payload) == []


def test_a_product_line_is_not_offered_as_a_model():
    """Supermicro's "Super Server" is already known to this codebase as a string that does not
    name a model, and offering it would contradict a judgement lumina has already made."""
    from lumina.results.models import GenericModel

    GenericModel.objects.get_or_create(vendor="Supermicro", product="Super Server")
    payload = {"summary": {"fields": {"dmi": {
        "system": [{"props": {"Product Name": "Super Server"}}],
        "baseboard": [{"props": {"Product Name": "X11DPi-N(T)"}}],
    }}}}

    assert _values(kind="motherboard", vendor="Supermicro", current="X11DPi-N",
                   payload=payload) == ["X11DPi-N(T)"]


def test_a_number_is_not_offered_as_a_name():
    """A board revision is "1.02", and a revision offered as a model reads as an answer."""
    # Seeded but off, since a revision offered as a model reads as an answer.
    NameSuggestionField.objects.filter(
        kind="motherboard", path="dmi.baseboard.*.Version").update(enabled=True, priority=5)
    payload = {"summary": {"fields": {"dmi": {"baseboard": [{"props": {
        "Product Name": "Mt. Collins", "Version": "1.02"}}]}}}}

    assert _values(kind="motherboard", vendor="FOXCONN", current="x", payload=payload) == [
        "Mt. Collins"]


def test_something_too_short_or_too_long_is_not_a_name():
    payload = {"summary": {"fields": {"dmi": {"processor": [{"props": {
        "Part Number": "Q8", "Version": "x" * 61}}]}}}}

    assert _values(payload=payload) == []


def test_the_same_value_twice_is_offered_once():
    """Two sockets of one CPU, or a board whose system and baseboard agree."""
    payload = {"summary": {"fields": {"dmi": {"processor": [
        {"props": {"Part Number": "Q80-30"}}, {"props": {"Part Number": "Q80-30"}}]}}}}

    assert _values(payload=payload) == ["Q80-30"]


def test_a_machine_with_no_dmi_is_offered_nothing():
    """An architecture with no SMBIOS, or a bundle from before the suite collected it. The page
    goes straight to the notes box rather than showing an empty list."""
    assert _values(payload={"summary": {}}) == []


def test_the_path_rides_along_unseen():
    """What turns the submitter's answer into a naming rule on the reviewer's side. Shown to
    nobody: a submitter who has to read it is being asked to understand the payload."""
    offered = naming.candidates(_context(), "cpu", "Neoverse-N1")

    assert offered[0]["path"] == "dmi.processor.0.Part Number"
    assert offered[0]["label"] == "Processor part number"


def test_a_pattern_does_not_run_past_a_dot():
    """``dmi.*`` must not mean every field of every structure, which is what fnmatch would make
    it and would put eleven strings in a list meant to hold two."""
    assert naming._path_matches("dmi.processor.*.Part Number", "dmi.processor.0.Part Number")
    assert not naming._path_matches("dmi.*", "dmi.processor.0.Part Number")


def test_a_wildcard_reads_whichever_one_has_a_value():
    """A two-socket machine reports two processor structures, and a rule about the silicon should
    not have to say which socket it means. The field list and the suggestion rows both offer ``*``,
    so a rule that disagreed with them about what a path means would be a trap."""
    payload = {"summary": {"fields": {"dmi": {"processor": [
        {"props": {"Part Number": ""}}, {"props": {"Part Number": "Q80-30"}}]}}}}

    context = naming.build_context("cpu", {}, payload)

    assert naming.lookup(context, "dmi.processor.*.Part Number") == "Q80-30"


def test_a_wildcard_over_nothing_is_not_a_match():
    context = naming.build_context("cpu", {}, {"summary": {}})

    assert naming.lookup(context, "dmi.processor.*.Part Number") is None


def test_a_rule_can_match_on_a_wildcard_path():
    payload = {"summary": {"fields": {"dmi": {"processor": [
        {"props": {"Socket Designation": "CPU0"}}, {"props": {"Part Number": "Q80-30"}}]}}}}
    _rule(kind="cpu", priority=1, match_field="dmi.processor.*.Part Number",
          model_pattern=r"^(Q80-30)$", build="Ampere Altra {{ match.1 }}")

    assert naming.apply_rules(naming.build_context("cpu", {}, payload))[0] == (
        "Ampere Altra Q80-30")


# --- boards keep their model in more than one place ---------------------------------


def test_a_board_that_keeps_its_model_in_the_version_field():
    """Reported: "some boards use the dmi field version to hold the model number". Gigabyte
    leaves Product Name as the literal string "Default string" and puts MZ32-AR0-00 in Version,
    so without this row the one machine that most needs an answer has none."""
    payload = {"summary": {"fields": {"dmi": {
        "baseboard": [{"props": {"Product Name": "Default string", "Version": "MZ32-AR0-00"}}],
        "system": [{"props": {"Product Name": "R282-Z90-00", "Version": "0100"}}],
    }}}}

    assert _values(kind="motherboard", vendor="Gigabyte", current="Default string",
                   payload=payload) == ["MZ32-AR0-00", "R282-Z90-00"]


def test_a_boards_product_name_filters_itself_out():
    """Why motherboards were offered nothing at all. A board *is* named by its baseboard Product
    Name, so that row always matches what the part is already called - which makes every other
    row the only one that can contribute."""
    payload = {"summary": {"fields": {"dmi": {"baseboard": [{"props": {
        "Product Name": "Mt. Collins", "Version": "ES1"}}]}}}}

    assert "Mt. Collins" not in _values(
        kind="motherboard", vendor="FOXCONN", current="Mt. Collins", payload=payload)


def test_a_board_revision_is_offered_but_says_what_it_is():
    """The cost of turning Version on: plenty of boards keep a bare revision there. Offered with
    its label, which is what tells a reader they are looking at a revision rather than a name."""
    payload = {"summary": {"fields": {"dmi": {"baseboard": [{"props": {
        "Product Name": "Mt. Collins", "Version": "ES1"}}]}}}}

    offered = naming.candidates(
        naming.build_context("motherboard", {"vendor": "FOXCONN", "model": "Mt. Collins"},
                             payload), "motherboard", "Mt. Collins")

    assert [(c["value"], c["label"]) for c in offered] == [("ES1", "Board version")]


def test_a_whitebox_that_names_the_board_in_the_system_version():
    """An integrator who filled in nothing else. Product Name and the board version are both
    placeholders, and the only real string on the machine is where nobody looks."""
    payload = {"summary": {"fields": {"dmi": {
        "baseboard": [{"props": {"Product Name": "To be filled by O.E.M.", "Version": "1.0"}}],
        "system": [{"props": {"Product Name": "Default string", "Version": "TRX40 DESIGNARE"}}],
    }}}}

    assert _values(kind="motherboard", vendor="OEM", current="To be filled by O.E.M.",
                   payload=payload) == ["TRX40 DESIGNARE"]


def test_every_field_carries_its_wildcard_form():
    """What a rule almost always wants: "whichever processor structure has a part number", not
    "the first one"."""
    fields = {f["path"]: f["wildcard"] for f in naming.available_fields(_cpu_context())}

    assert fields["dmi.processor.0.Part Number"] == "dmi.processor.*.Part Number"
    assert fields["cpu.model"] == "cpu.model", "nothing to widen"
