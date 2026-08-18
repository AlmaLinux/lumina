"""One dropdown for "who is validating this", where there used to be two.

Reported from the page: "I still see an option for validation level, even though I'm
submitting on behalf of a vendor. I thought we cleaned this up?"

The earlier cleanup removed *vendor* from the tier dropdown, on the reasoning that naming a
vendor already is the vendor claim. That stopped the two controls contradicting each other
but left the tier one inert: with a Dell membership the page preselected Dell for
"Submitting on behalf of" and then rendered a live "Validation level" dropdown offering
Community and AlmaLinux, and picking either changed nothing, because ``clean`` overwrote it.
Measured before changing anything:

    on_behalf_of: choices=['', 'dell'] initial='dell'
    claimed_validation_level: choices=[COMMUNITY, ALMALINUX] initial=None

They were one question asked twice. Now one ``attribution`` list: yourself, AlmaLinux where
applicable, and each vendor you may act for, with the vendor whose hardware it is
preselected. ``clean`` maps the answer back to the ``on_behalf_of`` /
``claimed_validation_level`` pair the rest of the pipeline already speaks, so ingest,
``effective_level``, and the audit entry needed no changes.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import Group, User
from django.urls import reverse

from lumina.core.certification import ValidationLevel
from lumina.results import ingest
from lumina.results.forms import RunListingProposalForm
from lumina.results.models import TestRun
from lumina.results.tests import factories as f
from lumina.vendors.models import Vendor, VendorMembership

pytestmark = pytest.mark.django_db

VENDOR = RunListingProposalForm.VENDOR_CHOICE_PREFIX


@pytest.fixture
def submitter():
    return User.objects.create_user("attr-sub", password="pw")


@pytest.fixture
def dell():
    return Vendor.objects.create(name="Dell Inc.", verified=True, published=True)


def _run(submitter):
    return ingest.ingest_bundle(
        submitter=submitter, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"],
            results=[f.validate_result("validate.cpu.functional")],
        ))),
    )


def _values(form):
    return [value for value, _ in form.fields["attribution"].choices]


# --- what the list contains -----------------------------------------------------


def test_a_plain_community_member_gets_no_dropdown_at_all(submitter):
    """One entry is not a choice. The old tier dropdown followed the same rule; this keeps
    it rather than rendering a list whose only option is the default."""
    form = RunListingProposalForm(run=_run(submitter), user=submitter)

    assert "attribution" not in form.fields


def test_a_vendor_member_gets_themselves_and_their_vendor(submitter, dell):
    VendorMembership.objects.create(
        user=submitter, vendor=dell, role=VendorMembership.ROLE_SUBMITTER,
    )

    form = RunListingProposalForm(run=_run(submitter), user=submitter)

    assert _values(form) == [ValidationLevel.COMMUNITY, f"{VENDOR}{dell.slug}"]


def test_a_sig_member_gets_almalinux(submitter):
    group, _ = Group.objects.get_or_create(name="certifier")
    submitter.groups.add(group)

    form = RunListingProposalForm(run=_run(submitter), user=submitter)

    assert _values(form) == [ValidationLevel.COMMUNITY, ValidationLevel.ALMALINUX]


def test_a_sig_member_who_is_also_a_vendor_gets_everything(submitter, dell):
    group, _ = Group.objects.get_or_create(name="certifier")
    submitter.groups.add(group)
    VendorMembership.objects.create(
        user=submitter, vendor=dell, role=VendorMembership.ROLE_SUBMITTER,
    )

    form = RunListingProposalForm(run=_run(submitter), user=submitter)

    assert _values(form) == [
        ValidationLevel.COMMUNITY, ValidationLevel.ALMALINUX, f"{VENDOR}{dell.slug}",
    ]


def test_the_old_two_dropdowns_are_gone(submitter, dell):
    """Not merely hidden. A leftover rendered field would be the reported bug again."""
    VendorMembership.objects.create(
        user=submitter, vendor=dell, role=VendorMembership.ROLE_SUBMITTER,
    )

    form = RunListingProposalForm(run=_run(submitter), user=submitter)

    assert "claimed_validation_level" not in form.fields
    assert "on_behalf_of" not in form.fields
    assert RunListingProposalForm.ATTRIBUTION_FIELDS == ("attribution",)


def test_a_vendor_slug_cannot_collide_with_a_tier(submitter):
    """Why vendor entries are prefixed. A vendor slugged "community" would otherwise be
    indistinguishable from the community tier.

    The machine has to actually be theirs, now that the list is filtered by relevance.
    """
    awkward = Vendor.objects.create(name="Community", verified=True, published=True)
    VendorMembership.objects.create(
        user=submitter, vendor=awkward, role=VendorMembership.ROLE_SUBMITTER,
    )
    inventory = f.default_inventory()
    inventory["summary"]["system"] = {
        "vendor": "Community", "product": "Box One", "kind": "prebuilt",
    }
    run = ingest.ingest_bundle(
        submitter=submitter, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"], inventory=inventory,
            results=[f.validate_result("validate.cpu.functional")],
        ))),
    )

    assert awkward.slug == "community"
    assert f"{VENDOR}community" in _values(RunListingProposalForm(run=run, user=submitter))


# --- the preselection -----------------------------------------------------------


def test_the_hardware_vendor_is_preselected(submitter, dell):
    """The case that prompted this: the run reports Dell hardware and the submitter is
    Dell, so Dell is the answer and nobody should have to say it."""
    VendorMembership.objects.create(
        user=submitter, vendor=dell, role=VendorMembership.ROLE_SUBMITTER,
    )

    form = RunListingProposalForm(run=_run(submitter), user=submitter)

    assert form.initial["attribution"] == f"{VENDOR}{dell.slug}"


def test_an_unverified_vendor_is_not_preselected(submitter):
    """Verification is what makes a vendor claim mean anything, so an unverified vendor
    must not become the default answer."""
    unverified = Vendor.objects.create(name="Dell Inc.", verified=False)
    VendorMembership.objects.create(
        user=submitter, vendor=unverified, role=VendorMembership.ROLE_SUBMITTER,
    )

    form = RunListingProposalForm(run=_run(submitter), user=submitter)

    assert form.initial.get("attribution") != f"{VENDOR}{unverified.slug}"


def test_a_saved_answer_is_remembered(submitter, dell):
    """Reopening the form must not silently revert to the default."""
    group, _ = Group.objects.get_or_create(name="certifier")
    submitter.groups.add(group)
    run = _run(submitter)
    run.claimed_validation_level = ValidationLevel.ALMALINUX
    run.save(update_fields=["claimed_validation_level"])

    form = RunListingProposalForm(run=run, user=submitter)

    assert form.initial["attribution"] == ValidationLevel.ALMALINUX


# --- what the answer does -------------------------------------------------------


def _post(client, run, attribution):
    return client.post(reverse("results:propose_listing", args=[run.uuid]), {
        "vendor_name": "Dell Inc.", "name": "PowerEdge R760",
        "machine_kind": "prebuilt", "attribution": attribution,
    })


def test_choosing_a_vendor_records_the_vendor_and_the_tier(client, submitter, dell):
    VendorMembership.objects.create(
        user=submitter, vendor=dell, role=VendorMembership.ROLE_SUBMITTER,
    )
    run = _run(submitter)
    client.force_login(submitter)

    _post(client, run, f"{VENDOR}{dell.slug}")

    run.refresh_from_db()
    assert run.on_behalf_of == dell
    assert run.claimed_validation_level == ValidationLevel.VENDOR


def test_choosing_yourself_records_no_vendor(client, submitter, dell):
    VendorMembership.objects.create(
        user=submitter, vendor=dell, role=VendorMembership.ROLE_SUBMITTER,
    )
    run = _run(submitter)
    client.force_login(submitter)

    _post(client, run, ValidationLevel.COMMUNITY)

    run.refresh_from_db()
    assert run.on_behalf_of is None
    assert run.claimed_validation_level == ValidationLevel.COMMUNITY


def test_switching_back_to_yourself_clears_a_stored_vendor(client, submitter, dell):
    """Changing your mind has to actually change it. Leaving the old vendor on the run
    would keep certifying as Dell after the submitter said not to."""
    VendorMembership.objects.create(
        user=submitter, vendor=dell, role=VendorMembership.ROLE_SUBMITTER,
    )
    run = _run(submitter)
    client.force_login(submitter)
    _post(client, run, f"{VENDOR}{dell.slug}")

    _post(client, TestRun.objects.get(pk=run.pk), ValidationLevel.COMMUNITY)

    run.refresh_from_db()
    assert run.on_behalf_of is None
    assert run.claimed_validation_level == ValidationLevel.COMMUNITY


def test_the_answer_does_not_pollute_the_listing_proposal(client, submitter, dell):
    """It is a control, not a fact about the hardware. The first version left
    ``"attribution": "vendor:dell-inc"`` in the stored blob."""
    VendorMembership.objects.create(
        user=submitter, vendor=dell, role=VendorMembership.ROLE_SUBMITTER,
    )
    run = _run(submitter)
    client.force_login(submitter)

    _post(client, run, f"{VENDOR}{dell.slug}")

    run.refresh_from_db()
    assert "attribution" not in run.listing_proposal
    assert "on_behalf_of" not in run.listing_proposal
    assert "claimed_validation_level" not in run.listing_proposal


def test_naming_a_vendor_you_do_not_represent_grants_nothing(client, submitter):
    """The form is a control, not the authority. ``effective_level`` re-derives the tier at
    approval from what the submitter may actually act for, so a crafted post gets the tier
    it is entitled to rather than the one it asked for."""
    from lumina.hardware.models import System
    from lumina.results.services import effective_level

    other = Vendor.objects.create(name="Supermicro", verified=True, published=True)
    run = _run(submitter)
    client.force_login(submitter)

    _post(client, run, f"{VENDOR}{other.slug}")

    run.refresh_from_db()
    listing = System.objects.create(vendor=other, name="Probe Box")
    assert effective_level(run, listing) == ValidationLevel.COMMUNITY


def test_the_page_renders_one_attribution_control(client, submitter, dell):
    VendorMembership.objects.create(
        user=submitter, vendor=dell, role=VendorMembership.ROLE_SUBMITTER,
    )
    run = _run(submitter)
    client.force_login(submitter)

    body = client.get(
        reverse("results:propose_listing", args=[run.uuid])
    ).content.decode()

    assert 'name="attribution"' in body
    assert 'name="claimed_validation_level"' not in body
    assert 'name="on_behalf_of"' not in body
    assert "Validating as" in body


def test_a_member_with_no_dropdown_still_records_community(client, submitter):
    """The plain-community path, where the field is not on the form at all.

    ``clean`` normalizes the absent answer to ``community`` rather than leaving the column
    blank. Blank is not a synonym: ``effective_level`` reads a run that claims nothing as
    "give this submitter the best tier they are entitled to", which happens to be community
    here but says something different about the run.

    Added because a mutation that skipped this branch entirely passed every other test in
    this file - the vendor clearing it also covers is done anyway by the view's
    ``pop("on_behalf_of", "")`` default, so nothing else noticed.
    """
    run = _run(submitter)
    client.force_login(submitter)

    client.post(reverse("results:propose_listing", args=[run.uuid]), {
        "vendor_name": "Dell Inc.", "name": "PowerEdge R760",
        "machine_kind": "prebuilt",
    })

    run.refresh_from_db()
    assert run.claimed_validation_level == ValidationLevel.COMMUNITY
    assert run.on_behalf_of is None


# --- only vendors the hardware could plausibly belong to ------------------------
#
# Reported: "We shouldn't offer the person to be able to use an irrelevant vendor as the
# 'on behalf of' target. Someone that represents Intel but a Dell system being submitted
# doesn't make much sense to list Intel."
#
# Exactly right, and the reason is what a certification claims. Intel made the processor;
# certifying the machine is a statement about the whole machine, which only its manufacturer
# is positioned to make. The preselection already knew this - ``vendor_to_attribute`` requires
# the hardware to resolve to that same vendor - but the *list* offered every vendor the person
# represented.


def _intel_member(submitter):
    # get_or_create: Intel is one of the three silicon vendors the reference-data migration
    # seeds, so a second row collides on the unique slug.
    intel, _ = Vendor.objects.get_or_create(
        name="Intel", defaults={"verified": True, "published": True},
    )
    VendorMembership.objects.create(
        user=submitter, vendor=intel, role=VendorMembership.ROLE_SUBMITTER,
    )
    return intel


def test_with_no_relevant_vendor_the_dropdown_disappears(submitter, dell):
    """Nothing left to choose between, so the control goes rather than offering one option -
    the same rule the rest of the form follows.

    Supermicro made neither this machine nor any part in it.
    """
    supermicro, _ = Vendor.objects.get_or_create(
        name="Supermicro", defaults={"verified": True, "published": True},
    )
    VendorMembership.objects.create(
        user=submitter, vendor=supermicro, role=VendorMembership.ROLE_SUBMITTER,
    )

    form = RunListingProposalForm(run=_run(submitter), user=submitter)

    assert "attribution" not in form.fields


def test_only_the_machines_makers_are_offered_here(submitter, dell):
    """The final shape. This control is about the machine, so it offers the machine's makers.

    It briefly offered component vendors, and that was wrong for a reason the data model could
    not fix: a field labelled "Validating as" on a form about a Dell system, offering
    "Intel - vendor certification", reads as Intel validating the Dell system. Intel's claim
    about their own CPU is made on the component row, where it can say which part it means.
    """
    VendorMembership.objects.create(
        user=submitter, vendor=dell, role=VendorMembership.ROLE_SUBMITTER,
    )
    intel = _intel_member(submitter)
    supermicro, _ = Vendor.objects.get_or_create(
        name="Supermicro", defaults={"verified": True, "published": True},
    )
    VendorMembership.objects.create(
        user=submitter, vendor=supermicro, role=VendorMembership.ROLE_SUBMITTER,
    )

    values = _values(RunListingProposalForm(run=_run(submitter), user=submitter))

    assert f"{VENDOR}{dell.slug}" in values
    assert f"{VENDOR}{intel.slug}" not in values
    assert f"{VENDOR}{supermicro.slug}" not in values


def test_an_unrecognised_manufacturer_does_not_restrict_anything(submitter):
    """The case where filtering would be actively harmful.

    A vendor submitting their own brand-new hardware reports a manufacturer string the
    catalog has never seen, so nothing resolves. Treating "cannot tell" as "nobody" would
    lock them out of attributing their own machine to themselves - and they are exactly who
    the vendor tier is for.
    """
    from lumina.results.services import identity_vendors

    acme = Vendor.objects.create(name="Acme Systems", verified=True, published=True)
    VendorMembership.objects.create(
        user=submitter, vendor=acme, role=VendorMembership.ROLE_SUBMITTER,
    )
    run = _run(submitter)          # reports Dell Inc., which is not in this database
    assert identity_vendors(run) == []

    form = RunListingProposalForm(run=run, user=submitter)

    assert f"{VENDOR}{acme.slug}" in _values(form)


def test_a_linked_listing_decides_relevance(submitter, dell):
    """Once a run is linked, a human has already said which catalog entry this machine is,
    and that entry names its manufacturer.

    The reported string is deliberately junk here. Written first with hardware that reported
    "Dell Inc." anyway, which resolved on its own and made the listing branch redundant -
    deleting that branch passed. This is the case only the listing can answer: firmware said
    "OEM", a reviewer recognised the machine and assigned it.
    """
    from lumina.hardware.models import System
    from lumina.results import ingest
    from lumina.results.services import assign_listing, identity_vendors
    from lumina.results.tests import factories as fac

    listing = System.objects.create(vendor=dell, name="PowerEdge R760")
    VendorMembership.objects.create(
        user=submitter, vendor=dell, role=VendorMembership.ROLE_SUBMITTER,
    )
    inventory = fac.default_inventory()
    # A named product from a manufacturer the catalog has never heard of, so the machine is a
    # prebuilt whose vendor does not resolve. It used to report a placeholder product and declare
    # itself prebuilt; with the kind derived, a placeholder product makes it a *custom* build and
    # the identity comes from the board - which is a Dell, and resolves.
    inventory["summary"]["system"] = {
        "vendor": "Nonesuch Systems", "product": "Box 9000",
    }
    run = ingest.ingest_bundle(
        submitter=submitter, source="api",
        bundle_file=fac.as_upload(fac.build_bundle(fac.make_report(
            run_types=["validate"], inventory=inventory,
            results=[fac.validate_result("validate.cpu.functional")],
        ))),
    )
    assert identity_vendors(run) == [], "the reported vendor must not resolve"

    assign_listing(run, system=listing, by=submitter)

    assert [v.slug for v in identity_vendors(TestRun.objects.get(pk=run.pk))] == [
        dell.slug
    ]


def test_a_custom_build_is_judged_on_its_board_maker(submitter):
    """A custom build has no system manufacturer; its board is its identity, so the board's
    maker is the one who could certify it.

    The system table is emptied on purpose. ``custom_build_inventory`` copies the board
    identity *into* the system table - that duplication is what makes the classifier call it
    custom - so with that fixture both branches read "ASRock" and judging a custom build on
    its system vendor passed. This reports the board alone.
    """
    from lumina.results import ingest
    from lumina.results.services import identity_vendors
    from lumina.results.tests import factories as fac

    asrock, _ = Vendor.objects.get_or_create(
        name="ASRock", defaults={"verified": True, "published": True},
    )
    inventory = fac.custom_build_inventory()
    inventory["summary"]["system"] = {"vendor": "", "product": "", "kind": "custom"}
    run = ingest.ingest_bundle(
        submitter=submitter, source="api",
        bundle_file=fac.as_upload(fac.build_bundle(fac.make_report(
            run_types=["validate"], inventory=inventory,
            results=[fac.validate_result("validate.cpu.functional")],
        ))),
    )
    assert run.system_vendor == "", "the system table must be empty for this to prove anything"

    assert [v.slug for v in identity_vendors(run)] == [asrock.slug]


def test_the_almalinux_option_is_untouched_by_the_filter(submitter):
    """It is not a vendor claim about the manufacturer, so relevance does not apply."""
    group, _ = Group.objects.get_or_create(name="certifier")
    submitter.groups.add(group)
    _intel_member(submitter)

    form = RunListingProposalForm(run=_run(submitter), user=submitter)

    assert ValidationLevel.ALMALINUX in _values(form)
