"""Firmware-string-to-listing mappings, so manual work is done once.

Run 71314765 is the case: a Lenovo server reports vendor "OEM" and product
"7D2XCTO1WW". A human works out it is a ThinkSystem SR645 and names the listing
accordingly - and every later run of that same machine reports the same
unhelpful strings, matches nothing, and asks the next submitter to work it out
again, possibly naming it differently and forking the catalog.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import Group, User

from lumina.hardware.models import Component, System
from lumina.results import ingest, services
from lumina.results.models import ReportedIdentityAlias
from lumina.results.tests import factories as f
from lumina.vendors.models import Vendor

pytestmark = pytest.mark.django_db


@pytest.fixture
def submitter():
    return User.objects.create_user("alias-sub", email="al@example.com")


@pytest.fixture
def reviewer():
    user = User.objects.create_user("alias-rev", email="alr@example.com")
    group, _ = Group.objects.get_or_create(name="reviewer")
    user.groups.add(group)
    return user


def _unbranded_run(submitter, **kw):
    """What the suite reports for firmware that names nothing useful - run 71314765 itself.

    No system model and no board manufacturer, which is why the aliases in this file key on an
    empty reported vendor. Its kind is ``custom`` - the fallback - and approving it without an
    answer still fails, because a custom build needs a board vendor to create a listing from.
    That is the case this whole file exists for: the one that has to ask a human.
    """
    inventory = f.default_inventory()
    inventory["summary"]["system"] = {"vendor": None, "product": None, "bios": {}}
    inventory["summary"]["baseboard"] = {"vendor": None, "product": "7D2XCTO1WW"}
    report = f.make_report(
        run_types=["validate"],
        results=[f.validate_result("validate.cpu.functional")],
        inventory=inventory,
        **kw,
    )
    return ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(f.build_bundle(report)),
        source="api",
    )


def _resolve_as_lenovo_system(run, reviewer):
    Vendor.objects.get_or_create(name="Lenovo", defaults={"slug": "lenovo"})
    run.listing_proposal = {"vendor_name": "Lenovo", "name": "ThinkSystem SR645",
                            "machine_kind": "prebuilt"}
    run.save(update_fields=["listing_proposal"])
    services.create_listings_from_run(run, by=reviewer)
    return System.objects.get(name="ThinkSystem SR645")


# --- recording ------------------------------------------------------------------


def test_resolving_a_run_records_what_its_firmware_strings_mean(submitter,
                                                               reviewer):
    run = _unbranded_run(submitter)
    system = _resolve_as_lenovo_system(run, reviewer)

    alias = ReportedIdentityAlias.objects.get(reported_product="7D2XCTO1WW")
    assert alias.listing == system
    assert alias.source_run == run
    assert alias.created_by == reviewer


def test_the_next_run_of_that_machine_links_itself(submitter, reviewer):
    """The whole point: the second submitter is not asked to re-derive it."""
    first = _unbranded_run(submitter)
    system = _resolve_as_lenovo_system(first, reviewer)

    second = _unbranded_run(submitter, run_id="22222222-3333-4444-5555-666666666666")

    # Already linked by ingest, which is the point: the submitter is never
    # asked, so there is nothing for them to answer differently.
    assert second.listing_system == system
    assert services.resolve_reported_system(second) == system
    assert services.missing_submission_details(second) == []


def test_a_board_alias_resolves_for_a_custom_build(submitter, reviewer):
    """A custom build is identified by its motherboard, and unbranded firmware
    reports no board manufacturer at all."""
    run = _unbranded_run(submitter)
    Vendor.objects.get_or_create(name="Lenovo", defaults={"slug": "lenovo"})
    run.listing_proposal = {"vendor_name": "Lenovo", "name": "ThinkSystem Board",
                            "machine_kind": "custom"}
    run.save(update_fields=["listing_proposal"])
    services.create_listings_from_run(run, by=reviewer)

    board = Component.objects.get(name="ThinkSystem Board")
    assert services.find_matching_board("", "7D2XCTO1WW") == board


def test_matching_is_case_insensitive(submitter, reviewer):
    """Firmware capitalization is not stable across BIOS revisions."""
    run = _unbranded_run(submitter)
    system = _resolve_as_lenovo_system(run, reviewer)

    assert ReportedIdentityAlias.resolve("", "7d2xcto1ww") == system
    assert ReportedIdentityAlias.resolve("", "  7D2XCTO1WW  ") == system


def test_no_alias_when_the_name_already_matches(submitter, reviewer):
    """find_matching_system finds those unaided; an alias would be noise."""
    Vendor.objects.get_or_create(name="Dell Inc.", defaults={"slug": "dell-inc"})
    report = f.make_report(
        run_types=["validate"],
        results=[f.validate_result("validate.cpu.functional")],
    )
    run = ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(f.build_bundle(report)),
        source="api",
    )
    run.listing_proposal = {"vendor_name": "Dell Inc.", "name": "PowerEdge R760"}
    run.save(update_fields=["listing_proposal"])
    services.create_listings_from_run(run, by=reviewer)

    assert not ReportedIdentityAlias.objects.filter(
        reported_product="PowerEdge R760"
    ).exists()


def test_a_blank_reported_product_records_nothing(submitter, reviewer):
    """A blank-to-listing mapping would claim every unidentifiable machine is
    this one."""
    inventory = f.default_inventory()
    inventory["summary"]["system"] = {"vendor": None, "product": None,
                                      "kind": "unknown", "bios": {}}
    inventory["summary"]["baseboard"] = {"vendor": None, "product": None}
    report = f.make_report(
        run_types=["validate"],
        results=[f.validate_result("validate.cpu.functional")],
        inventory=inventory,
    )
    run = ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(f.build_bundle(report)),
        source="api",
    )
    Vendor.objects.get_or_create(name="Lenovo", defaults={"slug": "lenovo"})
    run.listing_proposal = {"vendor_name": "Lenovo", "name": "Mystery Box",
                            "machine_kind": "prebuilt"}
    run.save(update_fields=["listing_proposal"])
    services.create_listings_from_run(run, by=reviewer)

    assert ReportedIdentityAlias.objects.count() == 0


def test_one_reported_identity_cannot_mean_two_things(submitter, reviewer):
    """Otherwise auto-linking would depend on row order."""
    from django.db import IntegrityError

    run = _unbranded_run(submitter)
    _resolve_as_lenovo_system(run, reviewer)
    other = System.objects.create(
        vendor=Vendor.objects.get(name="Lenovo"), name="Something Else",
        slug="something-else",
    )

    with pytest.raises(IntegrityError):
        ReportedIdentityAlias.objects.create(
            reported_vendor="", reported_product="7D2XCTO1WW",
            listing_system=other,
        )


# --- admin review ---------------------------------------------------------------


def test_admins_can_review_and_edit_the_mappings(client):
    """A mapping decides how every future run of that machine is classified, so
    a wrong one keeps being wrong until somebody can fix it."""
    from django.contrib import admin as dj

    assert ReportedIdentityAlias in dj.site._registry
    model_admin = dj.site._registry[ReportedIdentityAlias]
    assert model_admin.has_add_permission is not None
    # Editable, unlike the rest of the results admin, which is read-mostly.
    assert not getattr(model_admin, "readonly_fields", ()) or \
        "reported_product" not in model_admin.readonly_fields


def test_a_hand_written_mapping_works_without_any_run(reviewer):
    """A fleet whose firmware reports a machine-type code can be mapped before
    its first run is ever submitted."""
    lenovo = Vendor.objects.create(name="Lenovo", slug="lenovo")
    system = System.objects.create(vendor=lenovo, name="ThinkSystem SR665",
                                   slug="thinksystem-sr665")
    ReportedIdentityAlias.objects.create(
        reported_vendor="OEM", reported_product="7D2WCTO1WW",
        listing_system=system, created_by=reviewer,
        notes="Mapped from the fleet's purchase order.",
    )

    assert services.find_matching_system("OEM", "7D2WCTO1WW") == system


def test_removing_a_wrong_mapping_restores_the_old_behaviour(submitter, reviewer):
    run = _unbranded_run(submitter)
    _resolve_as_lenovo_system(run, reviewer)

    ReportedIdentityAlias.objects.all().delete()

    assert services.find_matching_system("", "7D2XCTO1WW") is None


# --- the kind correction is part of the mapping ---------------------------------


def _hp_style_run(submitter, **kw):
    """A prebuilt that fails to identify itself.

    HP mirrors the system name into the baseboard on the ProLiant line, so the
    mirror rule classifies it as a custom build even though its own system table
    names a real product.
    """
    inventory = f.default_inventory()
    inventory["summary"]["system"] = {"vendor": "HP", "product": "ProLiant DL360 Gen9",
                                      "kind": "custom", "bios": {}}
    inventory["summary"]["baseboard"] = {"vendor": "HP",
                                         "product": "ProLiant DL360 Gen9"}
    report = f.make_report(
        run_types=["validate"],
        results=[f.validate_result("validate.cpu.functional")],
        inventory=inventory, **kw,
    )
    return ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(f.build_bundle(report)),
        source="api",
    )


def test_a_stated_kind_correction_is_remembered(submitter, reviewer):
    """Nobody should have to keep telling the catalog that a ProLiant is a
    vendor system."""
    hp = Vendor.objects.create(name="HP", slug="hp")
    system = System.objects.create(vendor=hp, name="ProLiant DL360 Gen9",
                                   slug="hp-proliant-dl360-gen9")
    run = _hp_style_run(submitter)
    assert run.system_kind == "custom"

    services.assign_listing(run, system=system, by=reviewer,
                            machine_kind="prebuilt")

    alias = ReportedIdentityAlias.objects.get(reported_product="ProLiant DL360 Gen9")
    assert alias.resolved_kind == "prebuilt"
    assert alias.listing == system


def test_the_correction_applies_to_the_next_run(submitter, reviewer):
    hp = Vendor.objects.create(name="HP", slug="hp")
    system = System.objects.create(vendor=hp, name="ProLiant DL360 Gen9",
                                   slug="hp-proliant-dl360-gen9")
    first = _hp_style_run(submitter)
    services.assign_listing(first, system=system, by=reviewer,
                            machine_kind="prebuilt")

    second = _hp_style_run(submitter,
                           run_id="33333333-4444-5555-6666-777777777777")

    # The firmware still says custom; the mapping says otherwise.
    assert second.system_kind == "custom"
    assert second.effective_system_kind == "prebuilt"
    assert second.listing_system == system


def test_no_alias_when_nothing_needs_remembering(submitter, reviewer):
    """A well-behaved machine whose name matches and whose kind was detected
    correctly needs no mapping - one would duplicate working logic."""
    Vendor.objects.get_or_create(name="Dell Inc.", defaults={"slug": "dell-inc"})
    report = f.make_report(
        run_types=["validate"],
        results=[f.validate_result("validate.cpu.functional")],
    )
    run = ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(f.build_bundle(report)),
        source="api",
    )
    system = System.objects.create(
        vendor=Vendor.objects.get(name="Dell Inc."), name="PowerEdge R760",
        slug="dell-poweredge-r760-noalias",
    )

    services.assign_listing(run, system=system, by=reviewer)

    assert ReportedIdentityAlias.objects.count() == 0


def test_the_kind_is_not_inferred_from_the_listing_type(submitter, reviewer):
    """A System entry can be created for a custom build - a board cataloged as a
    system - so linking one proves nothing about the machine's kind."""
    asrock = Vendor.objects.create(name="ASRock", slug="asrock")
    board_as_system = System.objects.create(
        vendor=asrock, name="B650M PG Riptide", slug="asrock-b650m-as-system",
    )
    run = _hp_style_run(submitter)

    # Linked to a System, but nobody said what kind of machine it is.
    services.assign_listing(run, system=board_as_system, by=reviewer)

    alias = ReportedIdentityAlias.objects.filter(
        reported_product="ProLiant DL360 Gen9"
    ).first()
    assert alias is not None          # the listing mapping is still worth having
    assert alias.resolved_kind == ""  # but no kind was claimed


def test_a_reviewer_can_state_the_kind_on_the_assign_form(client, submitter,
                                                          reviewer):
    from lumina.results.forms import RunListingAssignForm

    run = _hp_style_run(submitter)
    form = RunListingAssignForm(run=run)

    assert "machine_kind" in form.fields
    values = [value for value, _ in form.fields["machine_kind"].choices]
    assert values == ["", "prebuilt", "custom"]


# --- a recorded correction applies to later runs of the same hardware ----------


def _alias(reported_vendor, reported_product, *, resolved_kind="", name="DL360"):
    """An alias row. Every alias points at a listing - the table's XOR
    constraint enforces that - so the kind correction rides along with one."""
    vendor, _ = Vendor.objects.get_or_create(name="HP", defaults={"slug": "hp"})
    system, _ = System.objects.get_or_create(
        vendor=vendor, name=name, defaults={"slug": name.lower().replace(" ", "-")},
    )
    return ReportedIdentityAlias.objects.create(
        reported_vendor=reported_vendor, reported_product=reported_product,
        resolved_kind=resolved_kind, listing_system=system,
    )


def _reported_run(submitter, *, kind, run_id, run_types=("validate",)):
    """A run whose firmware names an HP server but classifies it as a build."""
    inventory = f.default_inventory()
    inventory["summary"]["system"] = {"vendor": "HP", "product": "ProLiant DL360 Gen9",
                                      "kind": kind, "bios": {}}
    inventory["summary"]["baseboard"] = {"vendor": "HP",
                                         "product": "ProLiant DL360 Gen9"}
    results = ([f.validate_result("validate.cpu.functional")]
               if "validate" in run_types
               else [f.benchmark_result("bench.cpu.sysbench-multi", category="cpu")])
    report = f.make_report(run_types=list(run_types), run_id=run_id,
                           results=results, inventory=inventory)
    return ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(f.build_bundle(report)),
        source="api",
    )


def test_a_corrected_kind_carries_to_a_later_run_of_the_same_machine(submitter):
    """The landing page said "Custom build: HP ProLiant DL360 Gen9".

    A reviewer had already corrected that machine to prebuilt and the alias was
    recorded, which is the whole reason for keeping one. Nothing consulted it,
    so every later run of the server kept contradicting itself in public.
    """
    _alias("HP", "ProLiant DL360 Gen9", resolved_kind="prebuilt")
    run = _reported_run(submitter, kind="custom", run_types=("benchmark",),
                        run_id="baaaaaaa-0000-0000-0000-000000000001")

    assert run.system_kind == "custom"          # evidence is left alone
    assert run.effective_system_kind == "prebuilt"
    assert run.display_name == "HP ProLiant DL360 Gen9"
    assert "Custom build" not in run.display_name


def test_the_run_s_own_answer_still_outranks_the_alias(submitter):
    """A submitter describing this specific run is more current than a mapping."""
    _alias("HP", "ProLiant DL360 Gen9", resolved_kind="prebuilt")
    run = _reported_run(submitter, kind="custom",
                        run_id="baaaaaaa-0000-0000-0000-000000000002")
    run.listing_proposal = {"machine_kind": "custom"}

    assert run.effective_system_kind == "custom"


def test_an_alias_carrying_no_kind_leaves_detection_alone(submitter):
    """Most aliases only map an identity to a listing. That is not a kind claim."""
    _alias("HP", "ProLiant DL360 Gen9")
    run = _reported_run(submitter, kind="custom",
                        run_id="baaaaaaa-0000-0000-0000-000000000003")

    assert run.effective_system_kind == "custom"


def test_an_unrelated_machine_is_unaffected(submitter):
    """An alias for somebody else's machine changes nothing here. This machine's own detection is
    ``custom``: nothing in its firmware claims a vendor-built product, and custom is the
    fallback."""
    _alias("HP", "ProLiant DL360 Gen9", resolved_kind="prebuilt")
    other = _unbranded_run(submitter, run_id="baaaaaaa-0000-0000-0000-000000000004")

    assert other.effective_system_kind == "custom"


def test_the_board_identity_also_matches_an_alias(submitter):
    """A machine identified only by its board is exactly the alias case."""
    _alias("", "7D2XCTO1WW", resolved_kind="prebuilt", name="SR645")
    run = _unbranded_run(submitter, run_id="baaaaaaa-0000-0000-0000-000000000005")

    assert run.effective_system_kind == "prebuilt"


def test_apply_alias_kinds_prefills_the_same_answer(submitter):
    _alias("HP", "ProLiant DL360 Gen9", resolved_kind="prebuilt")
    runs = [
        _reported_run(submitter, kind="custom",
                      run_id=f"baaaaaaa-0000-0000-0000-00000000001{index}")
        for index in range(3)
    ]

    prefilled = services.apply_alias_kinds(runs)

    assert [run.effective_system_kind for run in prefilled] == ["prebuilt"] * 3
