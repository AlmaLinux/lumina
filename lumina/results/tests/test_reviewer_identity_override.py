"""The reviewer's correction of the machine a run will create.

Reported as: a reviewer has no way to override the submitted system name, or the
prebuilt/custom flag. Two different problems behind one sentence.

The **kind** control existed and worked. It rendered inside the collapsed block labelled
"Attest a different listing", because ``assignment_rows`` was "everything that is not a gate"
and swept it in - so the only control for a misdetected machine sat under a heading about a
decision nobody was making, and read as absent. That is precisely what had already happened to
the embargo gates, which is why they were moved out; this is the same move, one field along.

The **name** had no control at all. A reviewer's only remedy for a wrong one was to send the run
back and ask the submitter to retype it. That sat oddly beside ``RunComponentTiesForm``, which
lets a reviewer rewrite a *part's* vendor and model outright on the stated grounds that they are
"the last person who can fix it before approval creates the entries" - an argument that applies
at least as strongly to the machine, which is the listing the parts hang off.

The correction is written into ``listing_proposal`` rather than onto the run, which is what makes
the third piece fall out rather than needing building: ``create_listings_from_run`` reads the
proposal to name the listing, and ``record_identity_alias`` then keys the mapping on the reported
strings. So one edit both names what approval creates and teaches the catalog what this machine's
firmware means, with no second path to keep in step.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import Group, User
from django.urls import reverse

from lumina.audit.models import AuditLogEntry
from lumina.hardware.models import System
from lumina.results import ingest, services
from lumina.results.forms import RunListingAssignForm
from lumina.results.models import ReportedIdentityAlias, SystemKind, TestRun
from lumina.results.tests import factories as f
from lumina.results.tests.helpers import release
from lumina.vendors.models import Vendor

pytestmark = pytest.mark.django_db


@pytest.fixture
def submitter():
    return User.objects.create_user("ident-sub")


@pytest.fixture
def reviewer(client):
    user = User.objects.create_user("ident-rev")
    user.groups.add(Group.objects.get_or_create(name="reviewer")[0])
    client.force_login(user)
    return user


def _run(submitter, *, inventory=None, proposal=None) -> TestRun:
    run = ingest.ingest_bundle(
        submitter=submitter, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"], inventory=inventory or f.default_inventory(),
            results=[f.validate_result("validate.cpu.functional")],
        ))),
    )
    run = release(TestRun.objects.get(pk=run.pk))
    if proposal is not None:
        run.listing_proposal = proposal
        run.save(update_fields=["listing_proposal"])
    return run


def unhelpful_firmware() -> dict:
    """DMI that names nothing usable, which is the case the alias exists for.

    The default fixture reports "PowerEdge R760" - a perfectly good name - so a reviewer
    correcting a run of it is correcting the *submitter*, and ``record_identity_alias``
    rightly writes nothing: the ordinary matchers already find that listing unaided and the
    mapping would carry nothing.
    """
    inventory = f.default_inventory()
    inventory["summary"]["system"] = {
        **inventory["summary"]["system"], "vendor": "OEM", "product": "7D2XCTO1WW",
    }
    return inventory


def _assign(client, run, **fields):
    """POST the assign form. Every box it always carries is sent, because the endpoint reads a
    blank as a decision - posting a subset would clear the rest."""
    data = {"components": [], "vendor_name": "", "name": "", "model_number": "",
            "machine_kind": "", "claimed_validation_level": ""}
    data.update(fields)
    return client.post(reverse("review:run_assign_listing", args=[run.pk]), data)


# --- the name -----------------------------------------------------------------------


def test_a_reviewer_can_rename_the_machine(client, reviewer, submitter):
    """The report. The submitter's string is not the last word on what the catalog calls a
    machine, because the reviewer is the one who knows what the catalog already calls it."""
    run = _run(submitter, proposal={"vendor_name": "Dell Inc.", "name": "PowerEdge R760xx",
                                    "machine_kind": "prebuilt"})

    _assign(client, run, vendor_name="Dell Inc.", name="PowerEdge R760")

    run.refresh_from_db()
    assert run.listing_proposal["name"] == "PowerEdge R760"


def test_the_rename_is_what_approval_creates(client, reviewer, submitter):
    """The correction has to reach the catalog, not just the run. Storing it on the proposal is
    what puts it on the path ``create_listings_from_run`` already reads."""
    run = _run(submitter, proposal={"vendor_name": "Dell Inc.", "name": "0M83RH",
                                    "machine_kind": "prebuilt"})
    _assign(client, run, vendor_name="Dell Inc.", name="PowerEdge R760")
    run.refresh_from_db()

    services.approve_run(run, by=reviewer)
    listings = services.create_listings_from_run(run, by=reviewer)

    assert [listing.name for listing in listings] == ["PowerEdge R760"]
    assert not System.objects.filter(name="0M83RH").exists()


def test_the_vendor_can_be_corrected_too(client, reviewer, submitter):
    """A submitter who types "Dell" where the catalog says "Dell Inc." is the ordinary case, and
    the reviewer is who knows which."""
    run = _run(submitter, proposal={"vendor_name": "Dell", "name": "PowerEdge R760",
                                    "machine_kind": "prebuilt"})

    _assign(client, run, vendor_name="Dell Inc.", name="PowerEdge R760")

    run.refresh_from_db()
    assert run.listing_proposal["vendor_name"] == "Dell Inc."


def test_the_model_number_can_be_cleared(client, reviewer, submitter):
    """Blank means blank for the part code, unlike the two that name the machine. A submitter
    who guessed at one needs a way to have it removed, and there is no listing lost by it."""
    run = _run(submitter, proposal={"vendor_name": "Dell Inc.", "name": "PowerEdge R760",
                                    "model_number": "guessed-at", "machine_kind": "prebuilt"})

    _assign(client, run, vendor_name="Dell Inc.", name="PowerEdge R760", model_number="")

    run.refresh_from_db()
    assert run.listing_proposal["model_number"] == ""


def test_emptying_the_name_is_not_a_way_to_delete_an_identity(client, reviewer, submitter):
    """A machine has to be called something, so a blank box there is a slip rather than a
    decision - there is no listing to create at the end of it."""
    run = _run(submitter, proposal={"vendor_name": "Dell Inc.", "name": "PowerEdge R760",
                                    "machine_kind": "prebuilt"})

    _assign(client, run, vendor_name="", name="")

    run.refresh_from_db()
    assert run.listing_proposal["name"] == "PowerEdge R760"
    assert run.listing_proposal["vendor_name"] == "Dell Inc."


# --- the kind -----------------------------------------------------------------------


def test_a_reviewer_can_correct_the_machine_kind(client, reviewer, submitter):
    """The control that existed all along. Kept as a test of the behaviour, separate from the
    one asserting it is now somewhere a reviewer can find it."""
    run = _run(submitter, inventory=f.custom_build_inventory())
    assert run.effective_system_kind == SystemKind.CUSTOM

    _assign(client, run, name="ThinkSystem SR645", vendor_name="Lenovo",
            machine_kind="prebuilt")

    run.refresh_from_db()
    assert run.effective_system_kind == SystemKind.PREBUILT


def test_correcting_the_kind_changes_which_half_of_the_catalog_it_lands_in(
        client, reviewer, submitter):
    """What the flag is actually for: a vendor that mirrors its system name into the baseboard
    reads as a custom build, and would be filed as a motherboard."""
    run = _run(submitter, inventory=f.custom_build_inventory())
    _assign(client, run, vendor_name="ASRock", name="B650M PG Riptide",
            machine_kind="prebuilt")
    run.refresh_from_db()

    services.approve_run(run, by=reviewer)
    listings = services.create_listings_from_run(run, by=reviewer)

    # The *listing* is now a System. The board is still tied as one of the machine's parts,
    # which is a different statement and the right one either way.
    assert [type(listing) for listing in listings] == [System]
    assert listings[0].name == "B650M PG Riptide"


# --- the correction outlives the run ------------------------------------------------


def test_the_correction_is_remembered_against_the_firmware_strings(
        client, reviewer, submitter):
    """Otherwise every later run of the same machine re-proposes the wrong name and the next
    reviewer fixes it again - or does not, and the catalog forks."""
    run = _run(submitter, inventory=unhelpful_firmware(),
               proposal={"vendor_name": "OEM", "name": "7D2XCTO1WW",
                         "machine_kind": "prebuilt"})
    _assign(client, run, vendor_name="Lenovo", name="ThinkSystem SR645")
    run.refresh_from_db()
    services.approve_run(run, by=reviewer)

    services.create_listings_from_run(run, by=reviewer)

    alias = ReportedIdentityAlias.objects.get(reported_product="7D2XCTO1WW")
    assert alias.listing_system.name == "ThinkSystem SR645"


def test_a_later_run_of_the_same_machine_lands_on_the_correction(
        client, reviewer, submitter):
    """The alias proved by using it, rather than by asserting a row exists. A row nothing
    consults would satisfy the test above and change nothing for the next submitter."""
    first = _run(submitter, inventory=unhelpful_firmware(),
                 proposal={"vendor_name": "OEM", "name": "7D2XCTO1WW",
                           "machine_kind": "prebuilt"})
    _assign(client, first, vendor_name="Lenovo", name="ThinkSystem SR645")
    first.refresh_from_db()
    services.approve_run(first, by=reviewer)
    listings = services.create_listings_from_run(first, by=reviewer)

    second = _run(User.objects.create_user("later-sub"), inventory=unhelpful_firmware())

    assert second.listing_system == listings[0]


def test_the_correction_is_audit_logged(client, reviewer, submitter):
    """A reviewer overriding what a submitter typed is a decision about somebody else's
    submission, so it says who, what it was, and what the machine itself reported."""
    run = _run(submitter, inventory=unhelpful_firmware(),
               proposal={"vendor_name": "OEM", "name": "7D2XCTO1WW",
                         "machine_kind": "prebuilt"})

    _assign(client, run, vendor_name="Lenovo", name="ThinkSystem SR645")

    entry = AuditLogEntry.objects.get(action="test_run.identity_corrected")
    assert entry.actor == reviewer
    assert entry.after["corrected"]["name"] == "ThinkSystem SR645"
    # What the machine itself said, so the entry answers "corrected from what" without
    # anybody having to go and find the run's inventory.
    assert entry.after["reported"]["name"] == "7D2XCTO1WW"


def test_saving_without_changing_anything_logs_nothing(client, reviewer, submitter):
    """The boxes are prefilled, so every save posts the full identity back. Treating that as a
    correction would fill the trail with entries recording that nobody did anything."""
    run = _run(submitter, proposal={"vendor_name": "Dell Inc.", "name": "PowerEdge R760",
                                    "machine_kind": "prebuilt"})

    _assign(client, run, vendor_name="Dell Inc.", name="PowerEdge R760",
            machine_kind="prebuilt")

    assert not AuditLogEntry.objects.filter(action="test_run.identity_corrected").exists()


# --- prefilled, and clear about who supplied what ------------------------------------


def _rows(run) -> dict:
    return {row["field"].name: row for row in RunListingAssignForm(run=run).identity_rows}


def test_the_boxes_carry_what_the_submitter_submitted(submitter):
    """Prefilled rather than blank, so the boxes state what approving will do as well as
    offering to change it. Blank boxes under a read-only copy of the same three values read as
    two different answers, which is why the read-only rows went."""
    run = _run(submitter, inventory=unhelpful_firmware(),
               proposal={"vendor_name": "Lenovo", "name": "ThinkSystem SR645",
                         "model_number": "7D2X", "machine_kind": "prebuilt"})

    rows = _rows(run)

    assert rows["name"]["field"].field.initial == "ThinkSystem SR645"
    assert rows["vendor_name"]["field"].field.initial == "Lenovo"
    assert rows["model_number"]["field"].field.initial == "7D2X"
    assert rows["machine_kind"]["field"].field.initial == "prebuilt"


def test_a_box_the_submitter_left_alone_falls_back_to_the_firmware(submitter):
    """What approving would use, which is the submitter's answer where they gave one and the
    report's own strings otherwise - the same rule ``create_listings_from_run`` follows."""
    run = _run(submitter, proposal={})

    rows = _rows(run)

    assert rows["name"]["field"].field.initial == "PowerEdge R760"
    assert rows["vendor_name"]["field"].field.initial == "Dell Inc."


def test_a_submitted_override_is_marked_as_one(submitter):
    """The ask: a reviewer has to be able to see which of these a person typed over the
    machine. The value alone cannot say - "ThinkSystem SR645" looks identical whether DMI
    produced it or a submitter did."""
    run = _run(submitter, inventory=unhelpful_firmware(),
               proposal={"vendor_name": "Lenovo", "name": "ThinkSystem SR645",
                         "machine_kind": "prebuilt"})

    rows = _rows(run)

    assert rows["name"]["overridden"] is True
    assert rows["name"]["reported"] == "7D2XCTO1WW"
    # And it says so on the page, next to the box rather than in a legend somewhere.
    help_text = rows["name"]["field"].field.help_text
    assert "Submitter's correction" in help_text
    assert "7D2XCTO1WW" in help_text


def test_a_value_straight_from_the_firmware_is_not_marked_as_a_correction(submitter):
    """Otherwise every box is flagged and the flag means nothing."""
    run = _run(submitter, proposal={})

    rows = _rows(run)

    assert rows["name"]["overridden"] is False
    assert "Submitter's correction" not in rows["name"]["field"].field.help_text
    assert "PowerEdge R760" in rows["name"]["field"].field.help_text


def test_retyping_the_firmware_string_is_not_a_correction(submitter):
    """A submitter who confirmed what the report said changed nothing, and flagging it would
    send a reviewer looking for a disagreement that is not there."""
    run = _run(submitter, proposal={"vendor_name": "Dell Inc.", "name": "PowerEdge R760"})

    assert _rows(run)["name"]["overridden"] is False


def test_the_kind_is_marked_when_the_submitter_corrected_it(submitter):
    """The kind reads as a code on the wire and as a sentence on the page, so the row says
    which machine kind the firmware implied rather than printing "custom" at a reviewer."""
    run = _run(submitter, inventory=f.custom_build_inventory(),
               proposal={"machine_kind": "prebuilt"})

    row = _rows(run)["machine_kind"]

    assert row["overridden"] is True
    assert "a custom build" in row["field"].field.help_text


def test_the_help_text_is_not_stacked_up_by_a_second_read(submitter):
    """``identity_rows`` is a property and the template may read it more than once. The
    provenance is set in __init__ for that reason; reading it twice must not say it twice."""
    form = RunListingAssignForm(run=_run(submitter, proposal={}))

    first = {row["field"].name: row["field"].field.help_text for row in form.identity_rows}
    second = {row["field"].name: row["field"].field.help_text for row in form.identity_rows}

    assert first == second
    assert second["name"].count("From the machine") == 1


# --- offered only where it does something --------------------------------------------


def test_no_identity_boxes_when_approval_reuses_an_existing_listing(submitter):
    """``create_listings_from_run`` reuses that listing as it stands and renames nothing, so a
    box here would take a reviewer's correction and silently drop it - the failure the effect
    box was rebuilt to stop telling."""
    dell, _ = Vendor.objects.get_or_create(name="Dell Inc.", defaults={"published": True})
    System.objects.create(vendor=dell, name="PowerEdge R760", published=True)
    run = _run(submitter)

    form = RunListingAssignForm(run=run)

    assert form.identity_rows == []
    assert "name" not in form.fields


def test_no_identity_boxes_on_a_scoped_run(submitter):
    """A scoped run never produces a machine listing, so every one of these describes something
    approving it cannot touch."""
    inventory = f.default_inventory()
    run = _run(submitter, inventory=inventory)
    run.claim_scope = ["gpu"]
    run.save(update_fields=["claim_scope"])

    form = RunListingAssignForm(run=run)

    assert form.identity_rows == []
    assert "machine_kind" not in form.fields


def test_a_post_cannot_smuggle_an_identity_past_a_reused_listing(client, reviewer, submitter):
    """Removing the field also unbinds it, so the boxes being absent from the page is enforced
    rather than merely rendered."""
    dell, _ = Vendor.objects.get_or_create(name="Dell Inc.", defaults={"published": True})
    System.objects.create(vendor=dell, name="PowerEdge R760", published=True)
    run = _run(submitter, proposal={"vendor_name": "Dell Inc.", "name": "PowerEdge R760"})

    _assign(client, run, name="Something Else", vendor_name="Someone Else")

    run.refresh_from_db()
    assert run.listing_proposal["name"] == "PowerEdge R760"
