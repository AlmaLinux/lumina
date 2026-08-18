"""Machines whose firmware identifies nothing.

Run 71314765 was a Lenovo-MTM server (7D2X...) whose firmware left "OEM" in the
manufacturer fields. It arrived as "Custom build: OEM 7D2XCTO1WW" and the draft
page told the submitter "Nothing else is required - this hardware is already in
the catalog", which was false on all three counts: it was not a custom build, it
was not in the catalog, and details were very much required.

Such a machine's *kind* is now ``custom`` - there are two kinds and custom is the fallback - and
that changes none of what this file protects. What matters is that nothing identifies it, so the
form asks for everything and approval refuses to invent a listing. Both are keyed on the missing
identity rather than on the third kind that used to exist.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import Group, User
from django.urls import reverse

from lumina.hardware.models import Component, ComponentKind, System
from lumina.results import ingest, services
from lumina.results.tests import factories as f

pytestmark = pytest.mark.django_db


@pytest.fixture
def submitter():
    return User.objects.create_user("unident", email="u@example.com")


@pytest.fixture
def reviewer():
    user = User.objects.create_user("unident-rev", email="ur@example.com")
    group, _ = Group.objects.get_or_create(name="reviewer")
    user.groups.add(group)
    return user


def _unidentified_run(submitter):
    """What the suite now reports for unbranded firmware: nulls, not "OEM"."""
    inventory = f.default_inventory()
    inventory["summary"]["system"] = {
        "vendor": None, "product": None,
        "serial": None, "uuid": None,
        "bios": {"vendor": None, "version": "3.10", "date": "2026-02-01"},
    }
    inventory["summary"]["baseboard"] = {
        "vendor": None, "product": "7D2XCTO1WW", "version": None, "serial": None,
    }
    report = f.make_report(
        run_types=["validate"],
        results=[f.validate_result("validate.cpu.functional")],
        inventory=inventory,
    )
    return ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(f.build_bundle(report)),
        source="api",
    )


def test_an_unidentified_machine_is_asked_for_details(submitter):
    run = _unidentified_run(submitter)

    # Custom, the fallback: nothing here claims to be a vendor-built product. What makes this
    # machine special is that its board names no manufacturer either, so the board cannot serve
    # as the identity - which is what the form and the refusal below key on.
    assert run.system_kind == "custom"
    outstanding = services.missing_submission_details(run)
    assert outstanding, "a machine nothing identifies must not sail through"
    assert "does not identify its manufacturer" in outstanding[0]


def test_it_cannot_be_submitted_for_review_empty(submitter):
    run = _unidentified_run(submitter)
    with pytest.raises(services.ReviewError, match="Still needed"):
        services.submit_for_review(run, by=submitter)


def test_the_draft_page_does_not_claim_it_is_already_cataloged(client, submitter):
    """The message that started this: it asserted a fact nobody had checked."""
    run = _unidentified_run(submitter)
    client.force_login(submitter)

    body = client.get(run.get_absolute_url()).content.decode()

    assert "already in the catalog" not in body
    assert "does not report its manufacturer" in body


def test_the_form_asks_which_kind_of_machine_it_is(client, submitter):
    """Neither a system model nor a board maker was reported, so the catalog
    cannot tell which half it belongs in. Guessing would file it wrong."""
    run = _unidentified_run(submitter)
    client.force_login(submitter)

    body = client.get(reverse("results:propose_listing", args=[run.uuid])).content.decode()

    assert "What kind of machine is this?" in body
    assert "A vendor-built system" in body
    assert "A custom build" in body


def test_declaring_it_a_vendor_system_creates_a_system_listing(client, submitter,
                                                              reviewer):
    run = _unidentified_run(submitter)
    client.force_login(submitter)
    client.post(reverse("results:propose_listing", args=[run.uuid]), {
        "vendor_name": "Lenovo", "name": "ThinkSystem SR645",
        "model_number": "7D2XCTO1WW", "machine_kind": "prebuilt",
        "description": "", "vendor_spec_url": "",
    })
    run.refresh_from_db()
    assert services.missing_submission_details(run) == []

    services.create_listings_from_run(run, by=reviewer)

    system = System.objects.get(name="ThinkSystem SR645")
    assert system.model_number == "7D2XCTO1WW"
    assert run.listing_system == system


def test_declaring_it_a_custom_build_creates_a_motherboard_component(client,
                                                                    submitter,
                                                                    reviewer):
    run = _unidentified_run(submitter)
    client.force_login(submitter)
    client.post(reverse("results:propose_listing", args=[run.uuid]), {
        "vendor_name": "Supermicro", "name": "H13SSW",
        "model_number": "", "machine_kind": "custom",
        "description": "Barebones build", "vendor_spec_url": "",
    })
    run.refresh_from_db()

    services.create_listings_from_run(run, by=reviewer)

    board = Component.objects.get(name="H13SSW")
    assert board.kind == ComponentKind.motherboard.value
    assert board.description == "Barebones build"
    assert board in run.listing_components.all()
    # The board also identifies a System, so the build appears under Systems.
    from lumina.hardware.models import System
    system = System.objects.get(name="H13SSW")
    assert system.vendor.name == "Supermicro"
    assert run.listing_system == system


def test_no_listing_is_invented_without_the_answer(submitter, reviewer):
    """Half-filled proposals must not silently land in the wrong half of the
    catalog."""
    run = _unidentified_run(submitter)
    run.listing_proposal = {"vendor_name": "Lenovo", "name": "ThinkSystem SR645"}
    run.save(update_fields=["listing_proposal"])

    # Still refused, and on the data rather than on a classification: nothing here names the
    # machine and nobody has said which half of the catalog their answer belongs in. Without this
    # the identity would be filed as a *motherboard*, since custom is the fallback kind.
    with pytest.raises(services.ReviewError, match="has not said whether"):
        services.create_listings_from_run(run, by=reviewer)


def test_a_placeholder_vendor_never_becomes_a_manufacturer(submitter, reviewer):
    """The catalog gained a vendor called OEM before the suite stopped
    forwarding placeholder strings; nothing here should recreate one."""
    from lumina.vendors.models import Vendor

    run = _unidentified_run(submitter)
    run.listing_proposal = {"vendor_name": "Lenovo", "name": "ThinkSystem SR645",
                            "machine_kind": "prebuilt"}
    run.save(update_fields=["listing_proposal"])
    services.create_listings_from_run(run, by=reviewer)

    assert not Vendor.objects.filter(name__iexact="OEM").exists()
