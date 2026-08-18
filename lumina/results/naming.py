"""How a reported part becomes the name it is catalogued and counted under.

The rules live in ``ComponentNamingRule`` rows rather than in this file, which is the point: an
unidentifiable part used to mean a release. What is here is the machinery - the view of a run a
rule may read, the sandbox it renders in, and the order rules are tried in.

**The context does the analysis, the rule states the policy.** A template cannot loop over a
machine's other GPUs to work out whether the CPU brand string is describing this one, so the
context answers that first (``machine.unnamed_amd_gpus``) and the rule tests it. That division is
what lets the two built-in GPU rules be data rather than a special case in code.

Sandboxed on purpose. A reviewer authoring a rule is running something server-side, and
``SandboxedEnvironment`` is what makes the template a formatting language rather than an execution
one: no attribute access into dunder internals, no imports, no filesystem.
"""
from __future__ import annotations

import json
import re
from typing import Any

from jinja2 import TemplateError, Undefined
from jinja2.sandbox import SandboxedEnvironment

# Undefined renders as "" rather than raising, because a rule reading a field the machine did not
# report is the ordinary case and is what ``fallback`` exists for. A rule that is simply wrong
# produces an empty name, which is visible; one that raises would take the run's resolution with it.
_ENV = SandboxedEnvironment(undefined=Undefined, autoescape=False)
# The group the part's own fields land in: ``vendor``, ``model``, ``kind`` and the rest carry no
# prefix of their own, and "everything else" is not a heading anybody can use.
DEVICE_GROUP = "device"

CONDITION_OPS = {
    "eq": lambda got, want: _text(got) == _text(want),
    "ne": lambda got, want: _text(got) != _text(want),
    "icontains": lambda got, want: _text(want).lower() in _text(got).lower(),
    "not_icontains": lambda got, want: _text(want).lower() not in _text(got).lower(),
    "regex": lambda got, want: bool(re.search(str(want), _text(got), re.I)),
    "exists": lambda got, want: bool(got) is bool(want),
    "gt": lambda got, want: _number(got) > _number(want),
    "lt": lambda got, want: _number(got) < _number(want),
}


def _text(value) -> str:
    return "" if value is None else str(value)


def _number(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def template_error(template: str) -> str | None:
    """Why this template will not render, or None. Used by the admin before a rule is saved."""
    try:
        _ENV.from_string(template or "")
    except TemplateError as exc:
        return str(exc)
    return None


def conditions_error(conditions) -> str | None:
    """Why these conditions are not usable, or None."""
    if not isinstance(conditions, list):
        return 'Conditions are a list of {"field": ..., "op": ..., "value": ...}.'
    for clause in conditions:
        if not isinstance(clause, dict) or "field" not in clause:
            return 'Every condition needs a "field".'
        op = clause.get("op", "eq")
        if op not in CONDITION_OPS:
            return f"{op!r} is not an operator. One of: {', '.join(sorted(CONDITION_OPS))}."
    return None


def lookup(context: dict, path: str):
    """``cpu.model`` or ``dmi.processor.0.Part Number`` out of a context, or None.

    ``*`` in place of a list index means any of them: ``dmi.processor.*.Part Number`` is the part
    number of whichever processor structure has one. A machine with two sockets reports two, and a
    rule about the silicon should not have to say which socket it is talking about - nor should a
    reviewer have to, since the field list and the suggestion rows both offer ``*`` and a rule that
    disagreed with them about what a path means would be a trap.
    """
    value: Any = context
    for index, part in enumerate(parts := (path or "").split(".")):
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, (list, tuple)):
            if part == "*":
                rest = ".".join(parts[index + 1:])
                # The first that resolves to something, not the first that exists: an empty
                # Part Number on socket one must not hide a real one on socket two.
                for item in value:
                    found = lookup(item, rest) if rest else item
                    if found not in (None, ""):
                        return found
                return None
            try:
                value = value[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            value = getattr(value, part, None)
        if value is None:
            return None
    return value


def matches(rule, context: dict) -> re.Match | bool | None:
    """Whether this rule applies, and the regex match if it has a pattern.

    Returns the match object so the template can read its captures, ``True`` for a rule that
    matched without a pattern, and ``None`` for no match.
    """
    if rule.kind and rule.kind != context.get("kind"):
        return None
    for clause in rule.conditions or []:
        op = CONDITION_OPS[clause.get("op", "eq")]
        if not op(lookup(context, clause["field"]), clause.get("value")):
            return None
    if rule.model_pattern:
        # Against whichever field the rule names, defaulting to the part's own model. The pattern
        # lives in a field of its own rather than in the template because a template's string
        # literals go through Jinja's own escape rules first, where "\b" becomes a backspace and
        # "\s" is deprecated outright - so a regex written the obvious way would silently match
        # nothing, with nothing to say why.
        found = re.search(
            rule.model_pattern, _text(lookup(context, rule.match_field or "model")), re.I)
        return found or None
    return True


def render(template: str, context: dict, match=None) -> str:
    """One template against one context. Blank on anything that goes wrong.

    Blank rather than raising: a rule is configuration, and a bad one must not be able to stop a
    run resolving. The empty result falls through to the rule's fallback, then to the next rule.
    """
    if not (template or "").strip():
        return ""
    captures: dict = {}
    if isinstance(match, re.Match):
        # Keyed both ways, because ``{{ match.1 }}`` is how the field's own help text says to read
        # a capture and Jinja resolves that dotted digit as an integer subscript. With string keys
        # alone every rule written the documented way rendered empty, with nothing to say why.
        for index, value in enumerate([match.group(0), *match.groups()]):
            captures[index] = captures[str(index)] = value
        # Named groups too: ``(?P<model>...)`` read as ``{{ match.model }}`` says what the capture
        # is for, where a number says only where it sat.
        captures.update({
            name: value for name, value in match.groupdict().items() if value is not None})
    try:
        return " ".join(_ENV.from_string(template).render(**context, match=captures).split())
    except Exception:
        return ""


def apply_rules(context: dict, rules=None) -> tuple[str, Any]:
    """The name the rules give this part, and the rule that gave it. ``("", None)`` for none.

    Lowest priority first, and the first rule that produces a non-empty name wins. A rule that
    matches but renders empty - the field it wanted is not on this machine, and it has no usable
    fallback - is passed over rather than allowed to blank the name, so a specific rule that does
    not apply cannot mask a general one that would.
    """
    for rule in active_rules() if rules is None else rules:
        found = matches(rule, context)
        if found is None:
            continue
        for template in (rule.build, rule.fallback):
            name = render(template, context, found)
            if name:
                return name, rule
    return "", None


def active_rules() -> list:
    """The enabled rules in priority order, fetched once so a run's devices share one query."""
    from lumina.hardware.models import ComponentNamingRule

    return list(ComponentNamingRule.objects.filter(enabled=True).order_by("priority", "id"))


def build_context(kind: str, device: dict, payload: dict | None = None,
                  siblings: list | None = None) -> dict:
    """The view of a run a rule may read. An allowlist, not the payload.

    A template can compose a name out of anything the collector recorded and reach nothing else -
    not the submitter, not another run, not the request. That is the same instinct as
    ``SurveySegment.SEGMENTABLE_FIELDS``, for the same reason: this is authored by people, and
    what it can read has to be a decision rather than an accident.

    The derived entries are the interesting half. ``machine.unnamed_amd_gpus`` and
    ``model_is_generic`` are questions a template cannot ask - they need the machine's other
    devices, or a curated list - so the context answers them and a rule tests the answer. That is
    what lets the built-in GPU rules be rows rather than a special case in code.

    ``device`` is whatever identifies the part for its kind: a PCI device dict for a GPU or a NIC,
    and a plain ``{"vendor", "model"}`` pair for a CPU or a motherboard, which the firmware
    reports as strings and which sit on no bus the collector enumerates.
    """
    from lumina.hardware.models import ComponentKind
    from lumina.results.component_match import (
        GPU_MARKETING_RE,
        is_generic_gpu_name,
        normalize_cpu_model,
        normalize_gpu_model,
    )
    from lumina.results.pci_names import (
        gpu_identity,
        nic_identity,
        pci_device_id,
        pci_vendor_id,
    )

    summary = ((payload or {}).get("summary") or {})
    cpu = ((summary.get("cpus") or [{}])[0] or {})
    cpu_model = cpu.get("model") or ""
    ids = device.get("pci_ids") or {}
    if kind == ComponentKind.gpu.value:
        vendor, raw_model = gpu_identity(device)
        model = normalize_gpu_model(raw_model)
    elif kind == ComponentKind.nic.value:
        vendor, raw_model = nic_identity(device)
        model = raw_model
    else:
        # A CPU or a motherboard: a vendor and a model string off the run, with no PCI identity
        # and no bracket convention to read. ``model`` is what the rules and the catalog both see.
        vendor = device.get("vendor") or ""
        raw_model = device.get("model") or ""
        model = raw_model

    others = [g for g in (siblings or []) if isinstance(g, dict)]
    unnamed_amd = 0
    for other in others:
        other_vendor, other_model = gpu_identity(other)
        if ("amd" in _text(other_vendor).lower()
                and not GPU_MARKETING_RE.search(normalize_gpu_model(other_model))):
            unnamed_amd += 1

    return {
        "kind": kind,
        "vendor": vendor,
        "model": model,
        "raw_model": raw_model,
        "model_is_generic": is_generic_gpu_name(model),
        "model_is_marketing": bool(GPU_MARKETING_RE.search(model)),
        "pci": {
            "vendor_id": pci_vendor_id(device),
            "device_id": pci_device_id(device),
            "subsystem_vendor": ids.get("subsystem_vendor") or "",
            "subsystem_device": ids.get("subsystem_device") or "",
            "slot": device.get("pci") or "",
            "driver": device.get("driver") or "",
            "driver_version": device.get("driver_version") or "",
        },
        "cpu": {
            "model": cpu_model,
            # The catalog's spelling of the same string - marks, clock speed, and core count taken
            # off - so a GPU named after its CPU matches the CPU component's own name. Not a
            # naming judgement about graphics: that is what the ``re_sub`` filter is for, and a
            # rule that wants the iGPU trimmed off says so itself.
            "model_normalized": normalize_cpu_model(cpu_model),
            "vendor": cpu.get("vendor") or "",
            "cores": cpu.get("cores") or cpu.get("cores_per_socket") or "",
            "threads": cpu.get("threads") or cpu.get("threads_total") or "",
            "sockets": cpu.get("sockets") or "",
        },
        # Optional in the schema, and empty where lspci or dmidecode was unavailable. A rule reading
        # a field that is not there renders empty and falls through, which is how the part keeps the
        # name it would have had anyway.
        #
        # The flattened view: dmidecode's structures arrive wrapped in a handle, a type and a name,
        # and a rule wants the properties. Kept because every rule written so far addresses it.
        "dmi": {
            name: [entry.get("props") or {} for entry in entries]
            for name, entries in ((summary.get("fields") or {}).get("dmi") or {}).items()
        },
        # And everything the collector recorded, as it recorded it, whatever it is called. The
        # suite writes ``summary.fields`` as a namespace map precisely so a rule written a year
        # from now can read a field nobody thought worth promoting, on bundles submitted today -
        # and lumina was reaching in for ``dmi`` by name and dropping the rest, so ``fields.pci``
        # was already unreadable and a machine reporting its identity any other way (a device tree
        # rather than SMBIOS, say) could never be named at all.
        "fields": dict(summary.get("fields") or {}),
        "machine": {
            "gpus": len(others),
            "unnamed_amd_gpus": unnamed_amd,
        },
    }


# --- what a rule change would do to the catalog -----------------------------------


# The kinds whose resolution actually goes through the rules. One tuple rather than a check in
# each caller, so a kind joining is this and ``_component_names``, and the form stops warning
# about it on its own.
RULED_KINDS = ("gpu", "cpu", "motherboard")

# How many runs to name per conflicting name. Enough to see whether the disagreement is one odd
# machine or an even split, few enough to read. The rest are counted, not kept.
CONFLICT_EXAMPLES = 3


def _component_names(run, rules=None) -> list[tuple]:
    """``(component, new_name, rule)`` for the parts a run is evidence for.

    The rule is whichever produced the name, or None where no rule matched and the machine's own
    reported name stands. Carried because a plan that only says "these two names disagree" leaves
    a reviewer with nowhere to start, and because it is what a rename records on the component it
    renamed.

    Every kind in ``RULED_KINDS``. GPUs are enumerated off the bus and resolved per device; a CPU
    and a motherboard are a vendor and a model string on the run itself, which is why the two
    halves below look different.
    """
    from lumina.hardware.models import ComponentKind
    from lumina.results.services import cpu_brand

    return [
        *_gpu_names(run, rules),
        *_string_names(run, ComponentKind.cpu, cpu_brand(run), run.cpu_model, rules),
        *_string_names(run, ComponentKind.motherboard, run.board_vendor, run.board_model, rules),
    ]


def _string_names(run, kind, vendor: str, model: str, rules=None) -> list[tuple]:
    """The same, for a kind the firmware reports as two strings rather than as a device.

    One part per kind per run, so the component it belongs to is the one model-role component of
    that kind the run links to. No matching by name is needed or wanted: a CPU whose rule has
    already been applied no longer answers to the string the run reported.
    """
    from lumina.hardware.models import ComponentRole
    from lumina.results.component_match import catalog_name, name_status
    from lumina.results.services import _vendor_for

    if not model:
        return []
    linked = list(run.listing_components.filter(
        kind=kind.value, role=ComponentRole.MODEL))
    if len(linked) != 1:
        # None, or more than one and nothing says which. A family-role tie is the ordinary case
        # for a CPU and is deliberately not renamed: certification is granted per generation, and
        # a family is named by whoever curated it rather than by what one machine reported.
        return []
    status = name_status(kind, vendor, model, run.inventory or {}, rules)
    name = catalog_name(_vendor_for(vendor), status["name"], kind)
    return [(linked[0], name, status["rule"])]


def _gpu_names(run, rules=None) -> list[tuple]:
    """``(component, new_name, rule)`` for the GPUs a run is evidence for.

    The rule is whichever produced the name, or None where lumina's built-in naming did. Carried
    because a plan that only says "these two names disagree" leaves a reviewer with nowhere to
    start, and because it is what a rename records on the component it renamed.

    GPUs only, because they are the kind whose naming is wired to the rules. The shape is
    kind-agnostic on purpose: the day CPU resolution goes through the rules, it joins this list.
    """
    from lumina.hardware.models import ComponentKind, ComponentRole
    from lumina.results.component_match import catalog_name
    from lumina.results.exclusions import active_rules, exclusion_reason
    from lumina.results.services import _vendor_for, tieable_gpus

    # Model-role components only. A run links to the curated *family* where one exists, because
    # certification is granted per generation ("AMD Radeon RX 9000 Series (RDNA 4)"), and a family
    # is named by whoever curated it rather than by what any one machine reported. Renaming one to
    # a model name it happens to contain would flatten a generation into a single part, which is
    # what the plan proposed for three of its first four rows.
    linked = list(run.listing_components.filter(
        kind=ComponentKind.gpu.value, role=ComponentRole.MODEL))
    by_name = {component.name: component for component in linked}

    # ``exclusions``, not ``rules``: this function now takes a set of *naming* rules to ask about,
    # and the two shadowed each other - so every pair came back with no rule attached and every
    # applied rename recorded nothing about why.
    exclusions = active_rules()
    devices = tieable_gpus(run)
    # Read off the rules by PCI id rather than off the run's stored exclusions: those are keyed on
    # the device's *resolved* name and frozen at ingest, so the moment a naming rule changes that
    # name the stored key stops matching and a blacklisted part quietly becomes tieable again.
    blacklisted = {
        id(gpu): exclusion_reason(gpu, ComponentKind.gpu, rules=exclusions) is not None
        for gpu in devices
    }
    # The devices that could plausibly be a linked component, for the one-of-each case below.
    plausible = [gpu for gpu in devices if not blacklisted[id(gpu)]]

    pairs = []
    for gpu in devices:
        # The catalog's spelling, not the resolved string: creating a component strips the vendor
        # prefix a name repeats ("Intel Core Ultra 9 275HX" becomes "Core Ultra 9 275HX" under the
        # Intel vendor), so comparing the raw resolution against a component name finds nothing and
        # a plan built on it is silently empty.
        # Two names, and they are only the same thing when the question is about the rules that
        # are in force. ``current`` is what the catalog calls this part today and is how the
        # component is found; ``name`` is what the rules being asked about would call it.
        vendor = _vendor_for(gpu["vendor"])
        current = catalog_name(vendor, gpu["model"], ComponentKind.gpu)
        rule = (gpu.get("naming") or {}).get("rule")
        name = current
        if rules is not None:
            named, rule = apply_rules(
                build_context("gpu", gpu, run.inventory or {}, devices), rules)
            name = catalog_name(vendor, named or gpu["model"], ComponentKind.gpu)
        component = by_name.get(current) or _by_alias(linked, gpu.get("reported_model") or "")
        if component is None:
            if blacklisted[id(gpu)]:
                # Blacklisted and matching nothing this run attached: it is not evidence about any
                # catalogued part. Falling through to the guess below let a blacklisted BMC display
                # propose a name for the real card beside it, which held the whole plan in a
                # conflict that could not be resolved by editing any rule.
                #
                # Only when it matches nothing: a reviewer can tick a blacklisted part to include
                # it anyway, and a part that *is* catalogued is still named like any other.
                continue
            if len(linked) == 1 and len(plausible) == 1:
                # One component and one candidate device: it is that one, whatever either is
                # called, which is the case a rename has already happened in. With more than one
                # candidate, which device is which component is a guess.
                component = linked[0]
        if component is not None:
            pairs.append((component, name, rule))
    return pairs


def _by_alias(linked: list, reported: str):
    for component in linked:
        if reported and reported in (component.attributes or {}).get("aliases", []):
            return component
    return None


def rename_plan(runs=None) -> dict:
    """What applying the current rules would do to components already in the catalog.

    Three outcomes, and the split is the point. A **rename** is unambiguous: every run that
    produced this component agrees on the new name and nothing else is called that. A **collision**
    is a component that would take a name another component already has, which is a merge and a
    merge is a person's decision - the two may be the same part or may not, and nothing here can
    tell. A **conflict** is one component whose runs disagree, which usually means a rule is
    matching more broadly than its author meant.

    Nothing is written. This is what the preview reads and what the reviewer-side builder counts.
    """
    from lumina.hardware.models import Component
    from lumina.results.models import TestRun

    # Which runs produced each proposed name, not only the set of names. A conflict is the one
    # outcome a reviewer has to go and look at something to resolve, and "these two names
    # disagree" without saying where either came from is a dead end: the runs are the evidence,
    # and the run page is where the part can be renamed or a rule written.
    proposed: dict[int, dict[str, dict]] = {}
    for run in (runs if runs is not None else TestRun.objects.all()).iterator():
        for component, name, rule in _component_names(run):
            source = proposed.setdefault(component.pk, {}).setdefault(
                name, {"runs": [], "total": 0, "rule": rule})
            # Counted in full, kept in part: a part reported by four hundred runs needs a way in,
            # not four hundred links, and "and 396 more" is the number that tells a reviewer the
            # scale. Capping the list and deriving the count from it could only ever say "1 more".
            source["total"] += 1
            if len(source["runs"]) < CONFLICT_EXAMPLES:
                source["runs"].append(run)

    components = Component.objects.in_bulk(list(proposed))
    taken = {
        (component.kind, component.name.casefold()): component
        for component in Component.objects.filter(
            kind__in={c.kind for c in components.values()})
    }
    plan = {"renames": [], "collisions": [], "conflicts": [], "unchanged": 0}
    for pk, by_name in proposed.items():
        component = components.get(pk)
        if component is None:
            continue
        names = set(by_name)
        if len(names) > 1:
            plan["conflicts"].append({
                "component": component,
                "names": sorted(names),
                # Each name with the runs that produced it, which is what a reviewer opens.
                "sources": [
                    {"name": name,
                     "runs": by_name[name]["runs"],
                     "more": by_name[name]["total"] - len(by_name[name]["runs"]),
                     # Which rule produced this name, so the answer to "why does this run call it
                     # that" is on the row rather than something to go and work out.
                     "rule": by_name[name]["rule"]}
                    for name in sorted(names)
                ],
            })
            continue
        name = names.pop()
        if name == component.name:
            plan["unchanged"] += 1
            continue
        other = taken.get((component.kind, name.casefold()))
        if other is not None and other.pk != component.pk:
            plan["collisions"].append({"component": component, "name": name, "existing": other})
            continue
        plan["renames"].append({
            "component": component, "name": name, "rule": by_name[name]["rule"]})
    return plan


def apply_plan(plan: dict) -> int:
    """Perform the unambiguous renames of a plan. Returns how many were applied.

    Only the renames. A collision is left for a person and a conflict is left for whoever wrote
    the rule, and neither blocks the rest: one awkward part must not hold a good rule hostage.
    """
    from lumina.hardware.models import Component

    changed = []
    for row in plan["renames"]:
        component = row["component"]
        aliases = (component.attributes or {}).get("aliases", [])
        if component.name not in aliases:
            # The old name becomes an alias, so a run that reported it still matches this
            # component rather than creating a second one under the name it used to have.
            component.attributes = {**(component.attributes or {}),
                                    "aliases": [*aliases, component.name]}
        component.name = row["name"]
        # Which rule chose this name, recorded on the part. It was looked up in a mapping no
        # caller ever passed, so every applied rename recorded None and the catalog could not say
        # why anything was called what it was.
        component.named_by = row["rule"]
        changed.append(component)
    if changed:
        Component.objects.bulk_update(changed, ["name", "attributes", "named_by"])
    return len(changed)


# --- what a rule may read, and what it says on this machine ------------------------


def available_fields(context: dict) -> list[dict]:
    """Every readable path and its value on this machine, flattened for a form to offer.

    The point is the values. A reviewer writing a rule is looking at one part and deciding what
    about it is distinctive, and a list of field names alone makes them guess which one holds
    "8086" - so the suggestion shows the answer beside the name.

    Lists are indexed rather than summarized: ``dmi.processor.0.Part Number`` is the path a
    template and a condition both use, and offering ``dmi.processor`` would mean the reviewer
    working out the rest.
    """
    out: list[dict] = []

    def walk(prefix: str, value) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                walk(f"{prefix}.{key}" if prefix else str(key), item)
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                walk(f"{prefix}.{index}", item)
        elif prefix:
            out.append({
                "path": prefix,
                "value": "" if value is None else str(value),
                "expr": template_expression(prefix),
                # The same path with its list indices as wildcards, which is almost always what a
                # rule wants: "whichever processor structure has a part number", not "the first
                # one". A reader who only ever sees ``dmi.processor.0.Part Number`` has no way to
                # know the other form exists.
                "wildcard": re.sub(r"\.\d+(?=\.|$)", ".*", prefix),
            })

    walk("", context)
    return sorted(out, key=lambda entry: entry["path"])


def template_expression(path: str) -> str:
    """The path as a template has to write it, ready to insert.

    Most read as dotted names and one kind does not: a key with a space or a dash has to be
    subscripted, so ``dmi.processor.0.Part Number`` is written
    ``{{ dmi.processor.0["Part Number"] }}``. Which is which is not something a reviewer should
    have to know, so the field list hands them the answer rather than the path.
    """
    written = ""
    for part in (path or "").split("."):
        if not written:
            written = part
        elif part.isidentifier() or part.isdigit():
            written += f".{part}"
        else:
            written += f"[{json.dumps(part)}]"
    return "{{ " + written + " }}"


# A candidate has to be a plausible name. Under three characters is a code, not a name; over
# sixty is a sentence. Neither needs configuring, and both are what keep a list short enough to
# read without the reader doing the filtering themselves.
CANDIDATE_MIN = 3
CANDIDATE_MAX = 60


def candidates(context: dict, kind: str, current: str = "") -> list[dict]:
    """The values a submitter could be offered instead of the name a part has now.

    Values, never paths. Being asked which field holds the real model is being asked to understand
    the payload; being asked "is it this one or this one" is a question about the hardware in front
    of you. The path each value came from is carried along and shown to nobody, because it is what
    turns the answer into a naming rule on the reviewer's side.

    Which paths are worth offering is ``NameSuggestionField``, a row per kind and place, so the
    next machine that hides its model somewhere new is a row rather than a release. On top of that,
    filters that need no configuring: drop what the part is already called, what is too short or
    too long to be a name, what firmware leaves behind when nobody filled the field in, what names
    a product line rather than a model, what is only digits, and duplicates. An Ampere Altra
    reports eleven strings in its processor structure and two of them are worth being asked about.

    The junk filters are ``inventory_extract``'s own, not a second list: a machine whose System
    Product Name is "Super Server" is already known to this codebase as one that does not name a
    model, and offering the submitter that string as the answer would contradict a judgement
    lumina has already made about the same machine.
    """
    from lumina.hardware.models import NameSuggestionField
    from lumina.results.inventory_extract import is_generic_model, is_placeholder

    rows = NameSuggestionField.objects.filter(
        kind=getattr(kind, "value", kind), enabled=True).order_by("priority", "id")
    reported = available_fields(context)
    vendor = context.get("vendor") or ""
    seen = {" ".join((current or "").split()).casefold()}
    out: list[dict] = []
    for row in rows:
        for entry in reported:
            if not _path_matches(row.path, entry["path"]):
                continue
            value = " ".join((entry["value"] or "").split())
            if not (CANDIDATE_MIN <= len(value) <= CANDIDATE_MAX):
                continue
            if value.casefold() in seen:
                continue
            if is_placeholder(value) or is_generic_model(vendor, value):
                continue
            if not any(char.isalpha() for char in value):
                # "1.02", "A01" without the letter, a serial: a number is not a name, and a
                # version offered as a model is the confusing option this list exists to avoid.
                continue
            seen.add(value.casefold())
            out.append({"path": entry["path"], "value": value, "label": row.label})
    return out


def _path_matches(pattern: str, path: str) -> bool:
    """``dmi.processor.*.Part Number`` against ``dmi.processor.0.Part Number``.

    Segment by segment rather than with ``fnmatch``, whose ``*`` runs straight through the dots
    and would let ``dmi.*`` match every field of every structure.
    """
    wanted = pattern.split(".")
    got = path.split(".")
    return len(wanted) == len(got) and all(
        part == "*" or part == have for part, have in zip(wanted, got, strict=True))


def field_groups(fields: list[dict]) -> list[tuple[str, list[dict]]]:
    """The field list by its first segment, for a reader rather than a matcher.

    Two hundred flat paths is a list nobody scans. The question somebody writing a rule actually
    has is "what is there under ``pci``", and a group answers it. The part's own fields come first
    because they are what the rule is about; the rest read in name order.
    """
    groups: dict[str, list[dict]] = {}
    for entry in fields:
        head, dot, _rest = entry["path"].partition(".")
        groups.setdefault(head if dot else DEVICE_GROUP, []).append(entry)
    device = groups.pop(DEVICE_GROUP, None)
    return ([(DEVICE_GROUP, device)] if device else []) + sorted(groups.items())


def suggested_conditions(context: dict) -> list[dict]:
    """The conditions that identify the part in front of the reviewer, as a starting point.

    Prefilled rather than blank because the common case is tweaking what was detected, not
    composing from nothing: the ids are what make this part this part, and widening from there
    (deleting the device id to cover a vendor, say) is a smaller act than working out what to
    type. Only fields the machine actually reported.
    """
    wanted = ("pci.vendor_id", "pci.device_id")
    return [
        {"field": path, "op": "eq", "value": value}
        for path, value in ((path, lookup(context, path)) for path in wanted)
        if value
    ]
