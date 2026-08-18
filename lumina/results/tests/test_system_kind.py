"""Classifying a machine as prebuilt, custom, or unknown, from the strings its firmware reported.

Asked for: "let's identify system kind in lumina too and not the collector", following the
principle behind the GPU and NIC moves - "the collector shouldn't really make any decisions. It's
just collecting and reporting. Keeping the raw data and decisions server-side in lumina helps us
in case of issues where we need to reprocess data in some way."

This one is the clearest case for it. The rule is a *guess* about what firmware authors meant: it
reads placeholder junk, and it reads a system table that merely mirrors the motherboard because
the board maker filled it in and nobody overwrote it. Guesses get revised. Applied at collection
time, a revision reaches only future runs; applied here, it reaches every bundle ever submitted -
including 1.0 reports written before any classifier existed, which used to arrive permanently
"unknown".

The inputs below are the summaries the collector actually produces for each DMI fixture in
``tests/test_parsers.py``, read off rather than invented. Those tests still cover the parsing; the
classification moved here with them.
"""
from __future__ import annotations

import pytest

from lumina.results.inventory_extract import extract, is_placeholder, system_kind


def _kind(system, baseboard):
    return system_kind(system, baseboard)


# --- the cases that moved from the collector's tests -------------------------------


def test_a_system_table_mirroring_the_board_is_a_custom_build():
    """Board vendors copy the motherboard identity into the DMI system table on self-built
    machines; that mirror must not be mistaken for a vendor system model."""
    assert _kind(
        {"vendor": "ASRock", "product": "B650M PG Riptide"},
        {"vendor": "ASRock", "product": "B650M PG Riptide"},
    ) == "custom"


def test_a_distinct_system_model_is_a_prebuilt():
    assert _kind(
        {"vendor": "Dell Inc.", "product": "PowerEdge R760"},
        {"vendor": "Dell Inc.", "product": "0M83RH"},
    ) == "prebuilt"


def test_placeholder_junk_with_a_named_board_is_a_custom_build():
    """The MSI case: the system table is "To be filled by O.E.M." and the board names itself."""
    assert _kind(
        {"vendor": None, "product": None},
        {"vendor": "Micro-Star International Co., Ltd.",
         "product": "MPG X670E CARBON WIFI (MS-7D70)"},
    ) == "custom"


def test_nothing_at_all_is_a_custom_build():
    """Custom is the fallback. A machine is claimed to be a vendor-built system or it is not."""
    assert _kind({"vendor": None, "product": None}, {"vendor": None, "product": None}) == (
        "custom"
    )


def test_a_machine_type_code_plus_a_readable_model_is_a_prebuilt():
    """Regression, run 4f47867b. Lenovo stamps the MTM into both tables and puts the readable
    model in Version, so the run was listed as "LENOVO 21K9001NUS" and called a custom build.

    The mirror test runs against the *resolved* product name, which is why this reads as a
    product: somebody did write a model, it just was not in Product Name.
    """
    assert _kind(
        {"vendor": "LENOVO", "product": "ThinkBook 14 G6+ ABP"},
        {"vendor": "LENOVO", "product": "21K9001NUS"},
    ) == "prebuilt"


def test_a_chassis_does_not_decide_how_a_machine_was_built():
    """People build servers, and Framework ships laptops as kits, so a form factor cannot imply a
    vendor assembled the machine. This barebones Supermicro has no readable system model and is a
    custom build despite its rack chassis."""
    assert _kind(
        {"vendor": "Supermicro", "product": "H13SSW"},
        {"vendor": "Supermicro", "product": "H13SSW"},
    ) == "custom"


def test_an_unattributable_board_is_a_custom_build_too():
    """Run 71314765: a Lenovo server reporting no system model and no board manufacturer.

    This used to be a third kind, "unknown", on the reasoning that a machine whose firmware named
    nobody could be anything and calling it a self-build invented a fact. The reasoning was sound
    and the conclusion was in the wrong place: what mattered was not letting a listing be created
    out of a machine nothing identifies, and that is refused where listings are created - by name,
    naming the missing field. As a *kind*, there are two, and this is the fallback.
    """
    assert _kind(
        {"vendor": None, "product": None},
        {"vendor": None, "product": "7D2XCTO1WW"},
    ) == "custom"


def test_a_named_board_maker_is_a_custom_build_as_well():
    """Both reach the same answer now, by different routes: no system product means no claim to be
    a vendor-built product."""
    assert _kind(
        {"vendor": None, "product": None},
        {"vendor": "ASRock", "product": "7D2XCTO1WW"},
    ) == "custom"


# --- the placeholder rule ---------------------------------------------------------


@pytest.mark.parametrize("value", [
    None, "", "   ", "To Be Filled By O.E.M.", "Default string", "System Product Name",
    "Not Specified", "None", "OEM", "unknown", "0123456789", "-", "...",
])
def test_strings_vendors_leave_behind(value):
    assert is_placeholder(value) is True


@pytest.mark.parametrize("value", ["PowerEdge R760", "B650M PG Riptide", "0M83RH", "H13SSW"])
def test_real_names_are_not_placeholders(value):
    assert is_placeholder(value) is False


# --- and it is what the column gets ----------------------------------------------


def test_extract_derives_the_column():
    """Not read from the report. A bundle that carries the collector's old answer is ignored, so
    one rule decides for every run however old the bundle is."""
    inventory = {"summary": {
        # The collector's answer, deliberately wrong, and deliberately present.
        "system": {"vendor": "Dell Inc.", "product": "PowerEdge R760", "kind": "custom"},
        "baseboard": {"vendor": "Dell Inc.", "product": "0M83RH"},
    }}

    assert extract(inventory)["system_kind"] == "prebuilt"


def test_a_report_with_no_baseboard_table_still_classifies():
    """1.0 reports predate the baseboard table entirely. They used to arrive "unknown" because a
    1.0 collector never classified anything; now they are classified on their merits."""
    inventory = {"summary": {
        "system": {"vendor": "Dell Inc.", "product": "PowerEdge R760"},
    }}

    assert extract(inventory)["system_kind"] == "prebuilt"


# --- there is no third kind -------------------------------------------------------


def test_the_enum_offers_exactly_two():
    """Reported as the rule: "a system can either be claimed to be prebuilt, or vendor-built.
    There is no 'unknown' option. Custom built IS the fallback."
    """
    from lumina.results.models import SystemKind

    assert [value for value, _ in SystemKind.choices] == ["prebuilt", "custom"]


def test_the_column_defaults_to_the_fallback():
    """A row created without one - by hand, or in the admin - is a custom build rather than a
    value the field no longer offers."""
    from lumina.results.models import TestRun

    assert TestRun._meta.get_field("system_kind").default == "custom"


def test_nothing_classifies_as_anything_else():
    """Every combination of present and placeholder lands on one of the two."""
    values = [None, "", "OEM", "PowerEdge R760", "B650M PG Riptide"]
    seen = {
        _kind({"vendor": v1, "product": p1}, {"vendor": v2, "product": p2})
        for v1 in values for p1 in values for v2 in values for p2 in values
    }

    assert seen <= {"prebuilt", "custom"}
