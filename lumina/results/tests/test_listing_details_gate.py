"""Who may fill in listing details, and what that form is allowed to change.

Three rules, all reported together:

1. A run against hardware **already in the catalog** must not let the submitter restate what
   the listing is unless they speak for that hardware's vendor. A re-validation is evidence
   *about* a listing; it is not an occasion to redescribe it, and the identity fields would
   otherwise let any community member rewrite a manufacturer's own entry.

   Enforced on the fields, not on the door. It was the door - the whole page was refused -
   and that refused two people who needed it in turn: a component vendor claiming their own
   part inside somebody else's chassis, and then the submitter of a misidentified run, who
   by definition speaks for nothing about the listing they were matched to. The fields are
   rendered and locked, ``clean`` discards their values, and
   ``test_a_locked_post_changes_nothing`` is the test that matters.
2. ``description`` and ``vendor_spec_url`` belong to whoever maintains the listing. On a
   brand-new listing that is the submitter, so they stay. On one that already exists only
   the hardware's vendor sees them - and for them they now actually apply, which they
   never did before: both were written inline at ``System.objects.create`` and nowhere
   else, so any edit to an existing listing was accepted and then discarded. Removing
   them outright was the first attempt and it left the vendor with nowhere to describe
   their own machine.
3. AlmaLinux support can be added but never withdrawn, and the checkboxes have to say so.
   ``merge_listing_proposal`` has always unioned the majors, so unticking a box never
   actually retracted anything - which made the control a liar. It invited a reader to
   remove support, appeared to accept it, and quietly kept the claim.

Nothing is taken away from the refused submitter. ``missing_submission_details`` returns
nothing for a run against a known listing, so it submits with none of this, and the
release it validated on is recorded from the run itself by ``record_compatibility``, which
lowers a floor on earlier evidence. The claim moves from a form field to the evidence,
which is where it belonged.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.urls import reverse

from lumina.hardware.models import Component, ComponentKind, System
from lumina.results import ingest, services
from lumina.results.forms import RunListingProposalForm
from lumina.results.models import TestRun
from lumina.results.tests import factories as f
from lumina.results.tests.helpers import release
from lumina.vendors.models import Vendor, VendorMembership

pytestmark = pytest.mark.django_db


@pytest.fixture
def submitter():
    return User.objects.create_user("gate-sub", email="g@example.com")


@pytest.fixture
def reviewer():
    from django.contrib.auth.models import Group

    user = User.objects.create_user("gate-rev")
    group, _ = Group.objects.get_or_create(name="reviewer")
    user.groups.add(group)
    return user


def _run(submitter, version_id="9.6", **report_kw):
    return ingest.ingest_bundle(
        submitter=submitter, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"], version_id=version_id,
            results=[f.validate_result("validate.cpu.functional")],
            **report_kw,
        ))),
    )


def _member_of(user, vendor):
    VendorMembership.objects.create(
        user=user, vendor=vendor, role=VendorMembership.ROLE_SUBMITTER,
    )


# --- rule 1: who may detail a listing -------------------------------------------


def test_new_hardware_is_open_to_anyone(client, submitter):
    """The primary path. Somebody has to describe a machine the catalog has never seen,
    and requiring vendor membership for that would close the community route entirely."""
    run = _run(submitter)
    client.force_login(submitter)

    assert client.get(
        reverse("results:propose_listing", args=[run.uuid])
    ).status_code == 200


def _locked(user, run):
    return RunListingProposalForm(
        run=TestRun.objects.get(pk=run.pk), user=user, subject="system",
    ).identity_locked


def test_known_hardware_locks_the_identity_for_a_community_member(client, submitter):
    """The page opens - there are releases, components, and notes on it that are theirs - and
    what the machine *is* is not up for editing."""
    dell = Vendor.objects.create(name="Dell Inc.")
    System.objects.create(vendor=dell, name="PowerEdge R760")
    run = _run(submitter)
    client.force_login(submitter)

    resp = client.get(reverse("results:propose_listing", args=[run.uuid]))

    assert resp.status_code == 200
    assert "not yours to change" in resp.content.decode()
    assert _locked(submitter, run) is True


def test_a_locked_post_changes_nothing(client, submitter):
    """The one that carries the rule now. A collapsed field is not a permission check, so the
    values are discarded server-side."""
    dell = Vendor.objects.create(name="Dell Inc.")
    System.objects.create(vendor=dell, name="PowerEdge R760")
    run = _run(submitter)
    client.force_login(submitter)

    client.post(reverse("results:propose_listing", args=[run.uuid]), {
        "vendor_name": "Not Dell At All", "name": "Renamed Machine",
        "machine_kind": "prebuilt",
    })

    run.refresh_from_db()
    assert "name" not in run.listing_proposal
    assert "vendor_name" not in run.listing_proposal


def test_the_manufacturers_member_is_allowed(client, submitter):
    dell = Vendor.objects.create(name="Dell Inc.", verified=True)
    System.objects.create(vendor=dell, name="PowerEdge R760")
    _member_of(submitter, dell)
    run = _run(submitter)
    client.force_login(submitter)

    assert client.get(
        reverse("results:propose_listing", args=[run.uuid])
    ).status_code == 200
    assert _locked(submitter, run) is False


def test_membership_in_the_manufacturer_counts_not_only_the_owner(client, submitter):
    """``can_edit_listing`` binds to ``owner_vendor``, which a community-catalogued
    listing does not have - so reusing it here would refuse Dell on a Dell machine
    somebody else listed first. ``represents_listing_vendor`` asks the question the
    submitter is actually posing: is this my company's hardware?"""
    dell = Vendor.objects.create(name="Dell Inc.", verified=True)
    listing = System.objects.create(vendor=dell, name="PowerEdge R760")
    assert listing.owner_vendor_id is None, "the case under test"
    _member_of(submitter, dell)
    run = _run(submitter)

    assert _locked(submitter, run) is False


def test_membership_in_some_other_vendor_does_not_count(client, submitter):
    dell = Vendor.objects.create(name="Dell Inc.")
    System.objects.create(vendor=dell, name="PowerEdge R760")
    _member_of(submitter, Vendor.objects.create(name="Supermicro", verified=True))
    run = _run(submitter)

    assert _locked(submitter, run) is True


def test_a_custom_build_is_judged_on_its_motherboard(submitter):
    """A custom build's board *is* its listing, the same rule
    ``missing_submission_details`` applies."""
    asrock = Vendor.objects.create(name="ASRock")
    Component.objects.create(
        vendor=asrock, name="B650M PG Riptide",
        kind=ComponentKind.motherboard.value,
    )
    # ``custom_build_inventory`` reports this exact board in both DMI tables, which is
    # what makes the suite classify the machine as a custom build.
    run = _run(submitter, inventory=f.custom_build_inventory())

    assert _locked(submitter, run) is True

    _member_of(submitter, asrock)
    assert _locked(submitter, run) is False


def test_the_run_still_submits_with_no_details(client, submitter):
    """The reason refusing costs nothing. If this failed, the gate would strand every
    community re-validation in draft forever."""
    dell = Vendor.objects.create(name="Dell Inc.")
    System.objects.create(vendor=dell, name="PowerEdge R760")
    run = _run(submitter)

    assert services.missing_submission_details(run) == []
    services.submit_for_review(TestRun.objects.get(pk=run.pk), by=submitter)
    run.refresh_from_db()
    assert run.status == TestRun.STATUS_PENDING


def test_a_community_revalidation_still_records_its_major(client, submitter, reviewer):
    """Where the claim went. A community member cannot *say* a machine works on 9, but running
    the suite on 9 and having it approved proves it - and that is what marks the listing's row
    for 9 as proven rather than merely declared.

    So the capability the form used to offer is not lost, it moved to the evidence. This used
    to assert the sharper version of the same point, that the run *lowered the listing's minor
    floor*; hardware certifies per major now, and what a run still does is promote the row.
    """
    from lumina.hardware.models import ListingVersion
    from lumina.releases.models import AlmaLinuxRelease

    dell = Vendor.objects.create(name="Dell Inc.")
    listing = System.objects.create(vendor=dell, name="PowerEdge R760")
    nine, _ = AlmaLinuxRelease.objects.get_or_create(
        major=9, defaults={"supported": True},
    )
    declared = ListingVersion.objects.create(
        listing_system=listing, release=nine,
        source=ListingVersion.SOURCE_DECLARED,
    )

    run = _run(submitter, version_id="9.4")
    services.approve_run(release(run), by=reviewer)

    declared.refresh_from_db()
    assert declared.source == ListingVersion.SOURCE_RUN
    assert listing.versions.count() == 1


# --- rule 2: the two dropped fields ---------------------------------------------


def test_a_new_listing_still_takes_a_description(submitter):
    """Creating a listing means every field on the form is a new fact about a machine the
    catalog has never seen, description included.

    Removed for everybody at first, on a literal reading of "not things like the
    description". That was wrong in the case that matters most - it left a vendor with
    nowhere to describe their own hardware and pushed them into a second review round
    through ``hardware:propose_edit`` after publication.
    """
    run = _run(submitter)

    form = RunListingProposalForm(run=run, user=submitter)

    assert "description" in form.fields
    assert "vendor_spec_url" in form.fields


def test_a_community_revalidation_is_not_asked_for_prose(client, submitter):
    """On hardware that already exists these are somebody else's listing's fields, and
    they were never applied for a non-vendor anyway: they are written at
    ``System.objects.create`` time and nowhere else, so the prose was silently
    discarded."""
    dell = Vendor.objects.create(name="Dell Inc.")
    System.objects.create(vendor=dell, name="PowerEdge R760")
    run = _run(submitter)

    form = RunListingProposalForm(run=run, user=submitter)

    assert "description" not in form.fields
    assert "vendor_spec_url" not in form.fields


def test_the_vendor_keeps_them_on_their_own_listing(client, submitter):
    """The reported regression, from the vendor's seat: "I am dell (admin) and the
    description field disappeared"."""
    dell = Vendor.objects.create(name="Dell Inc.", verified=True)
    System.objects.create(vendor=dell, name="PowerEdge R760", owner_vendor=dell)
    _member_of(submitter, dell)
    run = _run(submitter)

    form = RunListingProposalForm(run=run, user=submitter)

    assert "description" in form.fields
    assert "vendor_spec_url" in form.fields


def test_the_vendors_description_reaches_an_existing_listing(
    client, submitter, reviewer
):
    """And it has to actually land. These two fields were write-once - set inline at
    ``System.objects.create`` and never again - so even the vendor's edit was accepted by
    the form, stored on the run, and then dropped."""
    dell = Vendor.objects.create(name="Dell Inc.", verified=True)
    listing = System.objects.create(
        vendor=dell, name="PowerEdge R760", owner_vendor=dell,
    )
    _member_of(submitter, dell)
    run = _run(submitter)
    client.force_login(submitter)
    client.post(reverse("results:propose_listing", args=[run.uuid]), {
        "vendor_name": "Dell Inc.", "name": "PowerEdge R760",
        "machine_kind": "prebuilt",
        "description": "2U dual-socket rack server.",
        "vendor_spec_url": "https://dell.example/r760",
    })

    services.approve_run(release(TestRun.objects.get(pk=run.pk)), by=reviewer)

    listing.refresh_from_db()
    assert listing.description == "2U dual-socket rack server."
    assert listing.vendor_spec_url == "https://dell.example/r760"


def test_a_blank_field_does_not_erase_what_is_there(client, submitter, reviewer):
    """A vendor submitting a second run without retyping the description must not wipe
    it. Blank means "no change", not "erase"."""
    dell = Vendor.objects.create(name="Dell Inc.", verified=True)
    listing = System.objects.create(
        vendor=dell, name="PowerEdge R760", owner_vendor=dell,
        description="Already written.",
    )
    _member_of(submitter, dell)
    run = _run(submitter)
    client.force_login(submitter)
    client.post(reverse("results:propose_listing", args=[run.uuid]), {
        "vendor_name": "Dell Inc.", "name": "PowerEdge R760",
        "machine_kind": "prebuilt", "description": "",
    })

    services.approve_run(release(TestRun.objects.get(pk=run.pk)), by=reviewer)

    listing.refresh_from_db()
    assert listing.description == "Already written."


def test_a_community_member_cannot_reach_the_write_path_directly(submitter):
    """The server-side half. Hiding the field is not a permission check, so
    ``apply_vendor_maintained_fields`` asks the same question again."""
    dell = Vendor.objects.create(name="Dell Inc.")
    listing = System.objects.create(
        vendor=dell, name="PowerEdge R760", description="Untouched.",
    )
    run = _run(submitter)
    run.listing_proposal = {"description": "I renamed your listing."}
    run.save(update_fields=["listing_proposal"])

    services.apply_vendor_maintained_fields(run, listing)

    listing.refresh_from_db()
    assert listing.description == "Untouched."


def test_the_identity_fields_are_still_never_applied_to_an_existing_listing(
    client, submitter, reviewer
):
    """Only the two maintenance fields got a write path. A vendor renaming a listing, or
    moving it to another manufacturer, still goes through review as an edit proposal."""
    dell = Vendor.objects.create(name="Dell Inc.", verified=True)
    listing = System.objects.create(
        vendor=dell, name="PowerEdge R760", owner_vendor=dell, model_number="R760",
    )
    _member_of(submitter, dell)
    run = _run(submitter)
    client.force_login(submitter)
    client.post(reverse("results:propose_listing", args=[run.uuid]), {
        "vendor_name": "Dell Inc.", "name": "Totally Different Name",
        "machine_kind": "prebuilt", "model_number": "XXXX",
    })

    services.approve_run(release(TestRun.objects.get(pk=run.pk)), by=reviewer)

    listing.refresh_from_db()
    assert listing.name == "PowerEdge R760"
    assert listing.model_number == "R760"


# --- rule 3: releases add, never remove -----------------------------------------


def test_an_unclaimed_release_is_editable(submitter):
    run = _run(submitter)

    form = RunListingProposalForm(run=run, user=submitter)

    assert form.fields["release_8"].disabled is False


def test_an_already_claimed_release_is_locked(client, submitter):
    """The control has to match the rule. The merge already refused to drop a major, so
    an editable box let a reader think they had withdrawn support when they had not."""
    dell = Vendor.objects.create(name="Dell Inc.", verified=True)
    System.objects.create(vendor=dell, name="PowerEdge R760")
    _member_of(submitter, dell)
    run = _run(submitter)
    client.force_login(submitter)
    client.post(reverse("results:propose_listing", args=[run.uuid]), {
        "vendor_name": "Dell Inc.", "name": "PowerEdge R760",
        "machine_kind": "prebuilt", "release_8": "on", "release_minor_8": "10",
    })

    form = RunListingProposalForm(run=TestRun.objects.get(pk=run.pk), user=submitter)

    assert form.fields["release_8"].disabled is True
    assert "not withdrawn" in form.fields["release_8"].help_text


def test_the_locked_box_renders_disabled(client, submitter):
    dell = Vendor.objects.create(name="Dell Inc.", verified=True)
    System.objects.create(vendor=dell, name="PowerEdge R760")
    _member_of(submitter, dell)
    run = _run(submitter)
    client.force_login(submitter)
    client.post(reverse("results:propose_listing", args=[run.uuid]), {
        "vendor_name": "Dell Inc.", "name": "PowerEdge R760",
        "machine_kind": "prebuilt", "release_8": "on", "release_minor_8": "10",
    })

    body = client.get(
        reverse("results:propose_listing", args=[run.uuid])
    ).content.decode()

    marker = body.index('name="release_8"')
    assert "disabled" in body[marker - 220:marker + 220]


def test_a_locked_release_survives_a_later_save(client, submitter):
    """The behaviour the lock describes. A disabled checkbox posts nothing, so this only
    holds because the view feeds ``initial`` to the bound form as well - without that the
    field cleaned to False and the major dropped out of the merge."""
    dell = Vendor.objects.create(name="Dell Inc.", verified=True)
    System.objects.create(vendor=dell, name="PowerEdge R760")
    _member_of(submitter, dell)
    run = _run(submitter)
    client.force_login(submitter)
    base = {
        "vendor_name": "Dell Inc.", "name": "PowerEdge R760",
        "machine_kind": "prebuilt",
    }
    client.post(reverse("results:propose_listing", args=[run.uuid]),
                dict(base, release_8="on"))

    # A second save that mentions 8 nowhere at all.
    client.post(reverse("results:propose_listing", args=[run.uuid]),
                dict(base, release_9="on"))

    run.refresh_from_db()
    assert run.listing_proposal["release_8"] is True
    assert run.listing_proposal["release_9"] is True


# ``test_a_locked_release_can_still_have_its_floor_widened`` stood here. It asserted that a
# locked major could have its minor floor lowered from 9.6 to 9.4, because adding support and
# broadening it are the same direction and only withdrawal is refused. There is no floor to
# broaden now - a major is the whole claim - so the case it covered cannot arise, and the
# "add, never remove" half of the rule is covered by the test above.


# --- a component vendor gets in, but cannot restate the machine -------------------
#
# Reported: "Now the ability to add details is completely gone. I only see 'submit for review'
# and 'Nothing else is required of you for this run.'"
#
# Correct, and self-inflicted. The per-component vendor claim - the one control built for an
# Intel engineer validating a Dell box - lived on a form gated entirely on the *machine's*
# vendor. Intel could never open it. Diagnosed on the reported run:
#
#     targets                  : Dell OptiPlex 3080 (vendor=Dell owner=None)
#     represents_listing_vendor: False
#     may_detail_listing       : False
#
# Access and permission-to-restate are two questions, and conflating them is what caused this.
# There is no access check left: the form opens for any run of one's own, and the identity
# fields are locked for anyone who does not speak for the machine's vendor.


def _intel_run(submitter, intel):
    """A run against a Dell machine already in the catalog, submitted by an Intel member."""
    from lumina.vendors.models import VendorMembership

    dell = Vendor.objects.create(name="Dell Inc.", published=True)
    System.objects.create(vendor=dell, name="PowerEdge R760")
    VendorMembership.objects.create(
        user=submitter, vendor=intel, role=VendorMembership.ROLE_SUBMITTER,
    )
    return _run(submitter)


@pytest.fixture
def intel():
    vendor, _ = Vendor.objects.get_or_create(
        name="Intel", defaults={"published": True},
    )
    vendor.verified = True
    vendor.save(update_fields=["verified"])
    return vendor


def test_a_component_vendor_may_open_the_form(client, submitter, intel):
    run = _intel_run(submitter, intel)
    client.force_login(submitter)

    assert client.get(
        reverse("results:propose_listing", args=[run.uuid])
    ).status_code == 200


def test_the_run_page_offers_it_to_them(client, submitter, intel):
    """The button, not just the endpoint. A reachable endpoint nobody is pointed at is the
    same bug from the user's side."""
    run = _intel_run(submitter, intel)
    client.force_login(submitter)

    body = client.get(run.get_absolute_url()).content.decode()

    assert reverse("results:propose_listing", args=[run.uuid]) in body


def test_they_get_the_component_claim_and_not_the_identity_fields(submitter, intel):
    """Both halves. The claim is the reason they are here; restating Dell's listing is not."""
    run = _intel_run(submitter, intel)

    form = RunListingProposalForm(run=run, user=submitter, subject="system")

    assert form.identity_locked is True
    for field in ("vendor_name", "name", "machine_kind", "model_number"):
        assert form.fields[field].required is False, field
    claimed = [row for row in form.component_rows if row.get("claim_field")]
    assert [row["kind"] for row in claimed] == ["cpu"]


def test_the_machines_own_vendor_still_gets_the_identity_fields(submitter):
    """Unchanged for whoever the listing belongs to."""
    from lumina.vendors.models import VendorMembership

    dell = Vendor.objects.create(name="Dell Inc.", verified=True, published=True)
    System.objects.create(vendor=dell, name="PowerEdge R760")
    VendorMembership.objects.create(
        user=submitter, vendor=dell, role=VendorMembership.ROLE_SUBMITTER,
    )
    run = _run(submitter)

    form = RunListingProposalForm(run=run, user=submitter, subject="system")

    assert form.identity_locked is False
    assert form.fields["name"].required is True


def test_a_bystander_gets_the_form_but_not_the_identity(client, submitter):
    """The rule survives the move: someone who speaks for neither the machine nor any part in it
    must not be able to restate a Dell listing. They still get the page, because the releases
    they validated, the parts to tie, and their own notes are theirs - and because the
    misidentification override has to be reachable by precisely this person."""
    dell = Vendor.objects.create(name="Dell Inc.", published=True)
    System.objects.create(vendor=dell, name="PowerEdge R760")
    run = _run(submitter)
    client.force_login(submitter)

    assert client.get(
        reverse("results:propose_listing", args=[run.uuid])
    ).status_code == 200
    assert _locked(submitter, run) is True


def test_the_identity_card_is_not_rendered_empty(client, submitter, intel):
    """Reported: "The 'Identity' block in the propose listing form is now completely blank."

    It was: the fields were dropped for a component vendor and the card rendered anyway, so the
    page showed an empty box with a heading. There is exactly one Identity heading now, and for
    a locked reader it belongs to the collapsed override block rather than to a bare card.
    """
    run = _intel_run(submitter, intel)
    client.force_login(submitter)

    body = " ".join(client.get(
        reverse("results:propose_listing", args=[run.uuid])
    ).content.decode().split())

    assert body.count('<h2 class="card-title">Identity</h2>') == 1
    heading = body.index('<h2 class="card-title">Identity</h2>')
    assert 'class="identity-override' in body[:heading], (
        "the only Identity heading must be the collapsed override, not an open card"
    )


def test_the_page_still_says_which_machine_it_is_about(client, submitter, intel):
    """Without the identity fields the form never named the hardware, which is disorienting on
    a page whose whole subject is one specific machine. And the reason they are missing is
    worth stating rather than left as an absence."""
    run = _intel_run(submitter, intel)
    client.force_login(submitter)

    body = " ".join(client.get(
        reverse("results:propose_listing", args=[run.uuid])
    ).content.decode().split())

    assert "PowerEdge R760" in body
    assert "not yours to change" in body


def test_the_card_is_still_there_for_the_machines_vendor(client, submitter):
    """The ordinary case must be untouched."""
    from lumina.vendors.models import VendorMembership

    dell = Vendor.objects.create(name="Dell Inc.", verified=True, published=True)
    System.objects.create(vendor=dell, name="PowerEdge R760")
    VendorMembership.objects.create(
        user=submitter, vendor=dell, role=VendorMembership.ROLE_SUBMITTER,
    )
    run = _run(submitter)
    client.force_login(submitter)

    body = " ".join(client.get(
        reverse("results:propose_listing", args=[run.uuid])
    ).content.decode().split())

    assert '<h2 class="card-title">Identity</h2>' in body
    assert 'name="name"' in body


def test_the_card_is_there_when_creating_a_listing(client, submitter):
    """New hardware is the primary path: every field is a new fact and the card is the form."""
    run = _run(submitter)
    client.force_login(submitter)

    body = " ".join(client.get(
        reverse("results:propose_listing", args=[run.uuid])
    ).content.decode().split())

    assert '<h2 class="card-title">Identity</h2>' in body
