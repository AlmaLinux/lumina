"""A rule somebody already saved keeps matching what it matched.

Vendor and the PCI ids were columns before they were conditions, and a rule's whole meaning lived
in them: drop the columns without reading them and a rule that named one card starts naming every
card of its kind, quietly, on a page nobody is looking at. Driven through Django's own migration
executor so it is the migration under test rather than a copy of its logic.
"""
from __future__ import annotations

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader

pytestmark = pytest.mark.django_db(transaction=True)

BEFORE = [("hardware", "0010_component_named_by")]
# Looked up rather than named: a fixture that put the database back to a middle state would leave
# every later test on the wrong schema.
AFTER = MigrationLoader(None, ignore_no_migrations=True).graph.leaf_nodes("hardware")


def _migrate(targets):
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(targets)
    executor.loader.build_graph()
    return executor.loader.project_state(targets).apps


@pytest.fixture
def at_0010():
    apps = _migrate(BEFORE)
    yield apps
    _migrate(AFTER)


def _rule(apps, **kwargs):
    model = apps.get_model("hardware", "ComponentNamingRule")
    kwargs.setdefault("build", "{{ model }}")
    return model.objects.create(**kwargs)


def _conditions(priority):
    apps = _migrate(AFTER)
    model = apps.get_model("hardware", "ComponentNamingRule")
    return model.objects.get(priority=priority).conditions


def test_a_vendor_column_becomes_a_substring_condition(at_0010):
    """What the column did: case-insensitive substring, not equality."""
    _rule(at_0010, kind="gpu", priority=100, vendor="NVIDIA")

    assert _conditions(100) == [{"field": "vendor", "op": "icontains", "value": "NVIDIA"}]


def test_the_pci_ids_become_exact_conditions(at_0010):
    _rule(at_0010, kind="gpu", priority=101, vendor_id="8086", device_id="7d67")

    assert _conditions(101) == [
        {"field": "pci.vendor_id", "op": "eq", "value": "8086"},
        {"field": "pci.device_id", "op": "eq", "value": "7d67"},
    ]


def test_carried_conditions_join_the_ones_already_written(at_0010):
    """A rule could have both, and losing either half changes what it matches."""
    _rule(at_0010, kind="gpu", priority=102, vendor_id="10de",
          conditions=[{"field": "model_is_generic", "op": "eq", "value": True}])

    assert _conditions(102) == [
        {"field": "model_is_generic", "op": "eq", "value": True},
        {"field": "pci.vendor_id", "op": "eq", "value": "10de"},
    ]


def test_a_rule_that_used_none_of_them_is_left_alone(at_0010):
    """The seeded rules are this shape, and rewriting them would make them differ from the code
    they reproduce."""
    _rule(at_0010, kind="gpu", priority=103,
          conditions=[{"field": "cpu.cores", "op": "gt", "value": 8}])

    assert _conditions(103) == [{"field": "cpu.cores", "op": "gt", "value": 8}]


def test_a_condition_already_written_by_hand_is_not_carried_twice(at_0010):
    """A reviewer could reach the same match either way, and a rule listing the same clause twice
    is a rule nobody can read."""
    _rule(at_0010, kind="gpu", priority=104, vendor_id="1002",
          conditions=[{"field": "pci.vendor_id", "op": "eq", "value": "1002"}])

    assert _conditions(104) == [{"field": "pci.vendor_id", "op": "eq", "value": "1002"}]


def test_backing_out_puts_the_values_back_in_the_columns(at_0010):
    """So the migration is reversible in the way a deployment actually needs."""
    _rule(at_0010, kind="gpu", priority=105, vendor="AMD", vendor_id="1002")
    _migrate(AFTER)

    apps = _migrate(BEFORE)
    rule = apps.get_model("hardware", "ComponentNamingRule").objects.get(priority=105)

    assert (rule.vendor, rule.vendor_id, rule.conditions) == ("AMD", "1002", [])


# --- the iGPU pattern moves out of code and into the rules -------------------------
#
# ``cpu.igpu_name`` and ``cpu.model_without_igpu`` were context fields computed by a module-level
# regex: one anticipated AMD phrasing, and any other spelling needed a release.

AT_0012 = [("hardware", "0012_naming_rule_match_field")]


@pytest.fixture
def at_0012():
    apps = _migrate(AT_0012)
    yield apps
    _migrate(AFTER)


def _built_in(apps, priority, **fields):
    """A seeded rule as it stood before this migration, then migrated forward.

    Created here rather than relied upon: these tests truncate between runs, so the rows the
    seeding migration wrote are long gone by the time one of them starts.
    """
    _rule(apps, kind="gpu", priority=priority, built_in=True, **fields)
    migrated = _migrate(AFTER)
    return migrated.get_model("hardware", "ComponentNamingRule").objects.get(
        built_in=True, priority=priority)


def test_the_amd_rule_carries_its_own_pattern_now(at_0012):
    rule = _built_in(at_0012, 10, build="{{ cpu.igpu_name }}")

    assert rule.match_field == "cpu.model"
    assert "Radeon" in rule.model_pattern
    assert rule.build == "{{ match.1 }}"


def test_the_amd_rule_no_longer_asks_for_the_removed_field(at_0012):
    """The condition asked whether the hardcoded pattern had found something. The rule asks the
    question itself."""
    rule = _built_in(at_0012, 10, build="{{ cpu.igpu_name }}",
                     conditions=[{"field": "cpu.igpu_name", "op": "exists", "value": True}])
    fields = {clause["field"] for clause in rule.conditions}

    assert "cpu.igpu_name" not in fields


def test_the_generic_rule_reads_the_normalized_cpu_name(at_0012):
    rule = _built_in(at_0012, 20, build="{{ cpu.model_without_igpu }} ({{ model }})")

    assert rule.match_field == "cpu.model_normalized"
    assert rule.build == "{{ match.1 }} ({{ model }})"


def test_somebody_elses_rule_is_pointed_at_a_field_that_still_exists(at_0012):
    """Left alone it would read an undefined path, render short, and rename their parts in
    silence."""
    _rule(at_0012, kind="gpu", priority=200, build="{{ cpu.model_without_igpu }} iGPU")

    apps = _migrate(AFTER)
    rule = apps.get_model("hardware", "ComponentNamingRule").objects.get(priority=200)

    assert rule.build == "{{ cpu.model_normalized }} iGPU"


def test_a_condition_on_a_removed_field_is_moved_too(at_0012):
    _rule(at_0012, kind="gpu", priority=201, build="x",
          conditions=[{"field": "cpu.igpu_name", "op": "exists", "value": True}])

    apps = _migrate(AFTER)
    rule = apps.get_model("hardware", "ComponentNamingRule").objects.get(priority=201)

    assert rule.conditions == [{"field": "cpu.model", "op": "exists", "value": True}]


def test_a_rule_that_never_read_those_fields_is_untouched(at_0012):
    _rule(at_0012, kind="gpu", priority=202, build="{{ model }} by {{ vendor }}",
          conditions=[{"field": "pci.vendor_id", "op": "eq", "value": "10de"}])

    apps = _migrate(AFTER)
    rule = apps.get_model("hardware", "ComponentNamingRule").objects.get(priority=202)

    assert rule.build == "{{ model }} by {{ vendor }}"
    assert rule.conditions == [{"field": "pci.vendor_id", "op": "eq", "value": "10de"}]
