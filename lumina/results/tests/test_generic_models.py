"""Generic product lines (Supermicro "Super Server") must not conflate into one listing.

A real, non-placeholder Product Name that names a *line* rather than a model is treated as
non-identifying: the machine is identified by its motherboard instead, so two Supermicros with
different boards become two Systems rather than one pooled "Super Server". The classification is
recomputed (``effective_system_kind``), so bundles already collected are handled without a re-run.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import Group, User

from lumina.hardware.models import System
from lumina.results import ingest, services
from lumina.results.inventory_extract import is_generic_model
from lumina.results.models import GenericModel, SystemKind
from lumina.results.tests import factories as f
from lumina.results.tests.helpers import release

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _releases():
    from lumina.releases.models import AlmaLinuxRelease
    for major in (8, 9, 10):
        AlmaLinuxRelease.objects.get_or_create(major=major, defaults={"supported": True})


@pytest.fixture
def submitter():
    return User.objects.create_user("gen-sub")


@pytest.fixture
def reviewer():
    user = User.objects.create_user("gen-rev")
    group, _ = Group.objects.get_or_create(name="reviewer")
    user.groups.add(group)
    return user


def _super_server_inventory(board="X11SCL-F"):
    inv = f.default_inventory()
    inv["summary"]["system"] = {
        "vendor": "Supermicro", "product": "Super Server",
        "serial": "0123456789", "uuid": "28d5b000-93b4-11e9-8000-ac1f6badb3c6",
        "bios": {"vendor": "American Megatrends", "version": "1.0", "date": "2024-01-01"},
    }
    inv["summary"]["baseboard"] = {
        "vendor": "Supermicro", "product": board, "version": "1.01",
        "serial": "VM196S011088",
    }
    return inv


def _ingest_super_server(submitter, board="X11SCL-F"):
    report = f.make_report(
        run_types=["validate"],
        results=[f.validate_result("validate.cpu.functional")],
        inventory=_super_server_inventory(board),
    )
    return ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(f.build_bundle(report)), source="api",
    )


def _approve_super_server(submitter, reviewer, board="X11SCL-F"):
    run = _ingest_super_server(submitter, board)
    # The submitter names the board, which is what the real form prefills for a board-identified
    # machine (effective_vendor/effective_product), not the generic system string.
    run.listing_proposal = {"vendor_name": "Supermicro", "name": board}
    run.save(update_fields=["listing_proposal"])
    services.approve_run(release(run), by=reviewer)
    return run


def test_is_generic_model_matches_the_seeded_line():
    assert is_generic_model("Supermicro", "Super Server") is True
    assert is_generic_model("supermicro", "super server") is True   # case-insensitive
    assert is_generic_model("Supermicro", "X11SCL-F") is False      # a real board is not generic
    assert is_generic_model("Dell Inc.", "PowerEdge R760") is False


def test_a_generic_product_is_recomputed_as_custom(submitter):
    """The stored kind stays the raw detection (prebuilt); effective_system_kind reinterprets it as
    custom, so an already-collected bundle is handled with no re-run."""
    run = _ingest_super_server(submitter)
    assert run.system_kind == SystemKind.PREBUILT          # raw evidence, frozen at ingest
    assert run.effective_system_kind == SystemKind.CUSTOM  # reinterpreted on the fly
    assert run.effective_product == "X11SCL-F"             # the board is the identity


def test_find_matching_system_refuses_a_generic_string():
    """Even if a "Super Server" System somehow exists, the generic string must never match it -
    that is the conflation guard."""
    from lumina.vendors.models import Vendor
    vendor = Vendor.objects.create(name="Supermicro")
    System.objects.create(vendor=vendor, name="Super Server", published=True)

    assert services.find_matching_system("Supermicro", "Super Server") is None


def test_two_super_servers_get_distinct_systems_by_board(submitter, reviewer):
    """The whole point: different boards under the same generic line become different Systems, not
    one pooled listing."""
    _approve_super_server(submitter, reviewer, board="X11SCL-F")
    _approve_super_server(User.objects.create_user("gen-sub2"), reviewer, board="H12SSL-i")

    names = set(System.objects.values_list("name", flat=True))
    assert "X11SCL-F" in names
    assert "H12SSL-i" in names
    assert "Super Server" not in names   # never a conflated line-level listing


def test_the_prompt_asks_a_generic_machine_for_its_real_model(client, submitter):
    """A generic-reported machine is told why it is listed by its board and asked for a real model
    if one exists - copy a genuine self-build does not get."""
    from django.urls import reverse

    run = _ingest_super_server(submitter)
    client.force_login(submitter)
    html = client.get(reverse("results:propose_listing", args=[run.uuid])).content.decode()

    assert "generic product line" in html
    assert "real vendor model" in html


def test_identity_overridden_flags_a_model_the_firmware_did_not_report(submitter):
    """The general review flag: a submitter-supplied model matching neither the detected system
    product nor the board is flagged; accepting a detected string is not. Not generic-only."""
    run = _ingest_super_server(submitter)    # system "Super Server", board "X11SCL-F"
    assert run.identity_overridden is False                          # nothing supplied yet
    run.listing_proposal = {"vendor_name": "Supermicro", "name": "SYS-621E-TR"}
    assert run.identity_overridden is True                           # a model neither string had
    run.listing_proposal = {"vendor_name": "Supermicro", "name": "X11SCL-F"}
    assert run.identity_overridden is False                          # accepted the detected board

    # General: a prebuilt whose submitter accepts vs corrects the detected model.
    report = f.make_report(
        run_types=["validate"], results=[f.validate_result("validate.cpu.functional")],
    )
    prebuilt = ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(f.build_bundle(report)), source="api",
    )
    assert prebuilt.system_product == "PowerEdge R760"
    prebuilt.listing_proposal = {"vendor_name": "Dell Inc.", "name": "PowerEdge R760"}
    assert prebuilt.identity_overridden is False                     # accepted
    prebuilt.listing_proposal = {"vendor_name": "Dell Inc.", "name": "PowerEdge R760xd"}
    assert prebuilt.identity_overridden is True                      # corrected -> flag


def test_the_review_queue_flags_a_submitter_named_run(client, submitter, reviewer):
    from django.urls import reverse

    run = _ingest_super_server(submitter)
    run.listing_proposal = {"vendor_name": "Supermicro", "name": "SYS-621E-TR"}
    run.save(update_fields=["listing_proposal"])
    release(run)   # -> pending, into the review queue

    client.force_login(reviewer)
    html = client.get(reverse("review:queue")).content.decode()
    assert "submitter-named" in html


def test_deconflate_splits_an_approved_super_server_by_board(submitter, reviewer):
    """The prod case: two boards already pooled onto one approved 'Super Server' System are split
    back out, each keeping its own certification, and the generic listing is removed."""
    from django.core.management import call_command

    from lumina.hardware.models import CommunityAttestation

    # Reproduce the old conflation: with "Super Server" not marked generic, both boards collapse
    # onto one System via the prebuilt name-match.
    GenericModel.objects.filter(product__iexact="Super Server").delete()
    services.approve_run(release(_ingest_super_server(submitter, "X11SCL-F")), by=reviewer)
    services.approve_run(
        release(_ingest_super_server(User.objects.create_user("ss2"), "H12SSL-i")), by=reviewer,
    )
    conflated = System.objects.get(name="Super Server")
    assert conflated.test_runs.count() == 2
    assert CommunityAttestation.objects.filter(listing_system=conflated).count() == 2

    # Mark it generic again and split it.
    GenericModel.objects.create(vendor="Supermicro", product="Super Server")
    call_command("deconflate_generic_systems")

    assert not System.objects.filter(name="Super Server").exists()
    x11 = System.objects.get(name="X11SCL-F")
    h12 = System.objects.get(name="H12SSL-i")
    assert x11.test_runs.count() == 1
    assert h12.test_runs.count() == 1
    # Each certification moved to the right board's System, none lost or pooled.
    assert CommunityAttestation.objects.filter(listing_system=x11).count() == 1
    assert CommunityAttestation.objects.filter(listing_system=h12).count() == 1


def test_two_super_servers_are_not_grouped_as_one_machine(submitter):
    """The submission bug: different boards under one generic line must not be siblings. Both
    "finish submission" (drafts) and the review "submit all N runs of this machine" key on this."""
    a = _ingest_super_server(submitter, "X11SCL-F")
    b = _ingest_super_server(submitter, "H12SSL-i")
    c = _ingest_super_server(submitter, "X11SCL-F")   # a second run of the same board = one machine

    siblings_of_a = list(services.sibling_draft_runs(a))
    assert c in siblings_of_a       # same board -> same machine, still grouped
    assert b not in siblings_of_a   # different board -> different machine, not grouped


def test_an_admin_added_generic_string_takes_effect(submitter):
    """Adding a row to the admin-editable list makes a new string generic (cache invalidated by the
    save signal). A blank vendor matches any manufacturer."""
    assert is_generic_model("Whitebox Co", "Server") is False
    GenericModel.objects.create(vendor="", product="Server", note="too generic")
    assert is_generic_model("Whitebox Co", "Server") is True
    assert is_generic_model("Anyone Else", "Server") is True


def test_the_form_prefills_a_generic_machine_as_a_custom_build(submitter):
    """The submission form for a generic-reported machine preselects 'custom build' and prefills the
    board as the name, not the generic system string, so the toggle and the fields agree."""
    from lumina.results.forms import RunListingProposalForm

    run = _ingest_super_server(submitter)   # system "Super Server", board "X11SCL-F"
    initial = RunListingProposalForm.initial_from_run(run)

    assert initial["machine_kind"] == SystemKind.CUSTOM
    assert initial["name"] == "X11SCL-F"
    assert initial["vendor_name"] == "Supermicro"
    assert initial["model_number"] == ""
