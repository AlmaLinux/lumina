"""The submitter's review of what the run reported, and what it counts as.

Three things meet here: the submitter can correct anything DMI got wrong, the
CPU is logged as a model with its family derived, and vendor attribution is
what makes a run vendor-validated.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import Group, User
from django.urls import reverse

from lumina.core.certification import ValidationLevel
from lumina.hardware.models import Component, ComponentKind, ComponentRole, System
from lumina.results import ingest, services
from lumina.results.forms import RunListingProposalForm
from lumina.results.tests import factories as f
from lumina.results.tests.helpers import release
from lumina.vendors.models import Vendor, VendorMembership
from lumina.vendors.services import derive_allowed_levels

pytestmark = pytest.mark.django_db


@pytest.fixture
def dell():
    return Vendor.objects.get_or_create(
        name="Dell Inc.", defaults={"slug": "dell-inc", "verified": True}
    )[0]


@pytest.fixture
def submitter():
    return User.objects.create_user("reviewer-sub", email="rs@example.com")


@pytest.fixture
def reviewer():
    user = User.objects.create_user("rev2", email="rev2@example.com")
    group, _ = Group.objects.get_or_create(name="reviewer")
    user.groups.add(group)
    return user


def _run(submitter, **report_kw):
    report = f.make_report(
        run_types=["validate"],
        results=[f.validate_result("validate.cpu.functional")],
        **report_kw,
    )
    return ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(f.build_bundle(report)),
        source="api",
    )


# --- who can certify on AlmaLinux's behalf -------------------------------------


def test_a_plain_user_can_only_claim_community(submitter):
    assert derive_allowed_levels(submitter, vendor=None) == [ValidationLevel.COMMUNITY]


def test_the_certifier_group_grants_almalinux_without_superuser(submitter):
    """A SIG member needs to certify on AlmaLinux's behalf, not to administer
    the application. Before this, "admin" was the only group that granted it,
    and the OIDC layer escalates that group to is_superuser."""
    group, _ = Group.objects.get_or_create(name="certifier")
    submitter.groups.add(group)

    assert ValidationLevel.ALMALINUX in derive_allowed_levels(submitter, vendor=None)
    submitter.refresh_from_db()
    assert submitter.is_superuser is False
    assert submitter.is_staff is False


def test_admin_still_grants_it(submitter):
    group, _ = Group.objects.get_or_create(name="admin")
    submitter.groups.add(group)
    assert ValidationLevel.ALMALINUX in derive_allowed_levels(submitter, vendor=None)


# --- vendor attribution --------------------------------------------------------


def test_a_vendor_member_submitting_their_own_hardware_is_preselected(submitter,
                                                                     dell):
    """DMI says Dell, the submitter is a Dell member, so the run is Dell's
    validation of Dell hardware."""
    VendorMembership.objects.create(
        user=submitter, vendor=dell, role=VendorMembership.ROLE_OWNER
    )
    run = _run(submitter)          # default inventory is a Dell PowerEdge R760

    assert RunListingProposalForm.vendor_to_attribute(run) == dell
    # Preselection moved onto the field itself when the two dropdowns became one, so that
    # the run is read in exactly one place.
    form = RunListingProposalForm(run=run, user=submitter)
    assert form.initial["attribution"] == (
        f"{RunListingProposalForm.VENDOR_CHOICE_PREFIX}{dell.slug}"
    )


def test_a_vendor_member_submitting_someone_elses_hardware_is_not(submitter, dell):
    """A Dell employee validating a Supermicro box is not Dell validating it."""
    VendorMembership.objects.create(
        user=submitter, vendor=dell, role=VendorMembership.ROLE_OWNER
    )
    inventory = f.default_inventory()
    inventory["summary"]["system"].update({"vendor": "Supermicro",
                                           "product": "SYS-221H-TN24R"})
    run = _run(submitter, inventory=inventory)

    assert RunListingProposalForm.vendor_to_attribute(run) is None


def test_an_unverified_vendor_grants_nothing(submitter):
    unverified = Vendor.objects.create(name="Dell Inc.", slug="dell-inc",
                                       verified=False)
    VendorMembership.objects.create(
        user=submitter, vendor=unverified, role=VendorMembership.ROLE_OWNER
    )
    run = _run(submitter)
    assert RunListingProposalForm.vendor_to_attribute(run) is None


def test_a_non_member_gets_no_attribution(submitter, dell):
    run = _run(submitter)
    assert RunListingProposalForm.vendor_to_attribute(run) is None


def test_attribution_makes_the_run_vendor_validated(submitter, dell, reviewer):
    """The point of the whole mechanism, and it did not work before: a listing
    created from a run has no owner_vendor yet, so effective_level had nothing
    to go on and capped every run at community."""
    VendorMembership.objects.create(
        user=submitter, vendor=dell, role=VendorMembership.ROLE_OWNER
    )
    run = _run(submitter)
    run.on_behalf_of = dell
    run.listing_proposal = {"vendor_name": "Dell Inc.", "name": "PowerEdge R760"}
    run.save(update_fields=["on_behalf_of", "listing_proposal"])
    services.create_listings_from_run(run, by=reviewer)

    system = System.objects.get(name="PowerEdge R760")
    assert services.effective_level(run, system) == ValidationLevel.VENDOR
    # and the vendor owns the listing, so they can maintain it afterwards
    assert system.owner_vendor == dell

    services.approve_run(release(run), by=reviewer)
    system.refresh_from_db()
    assert system.validation_level == ValidationLevel.VENDOR


def test_without_attribution_the_same_run_is_community(submitter, dell, reviewer):
    VendorMembership.objects.create(
        user=submitter, vendor=dell, role=VendorMembership.ROLE_OWNER
    )
    run = _run(submitter)
    run.listing_proposal = {"vendor_name": "Dell Inc.", "name": "PowerEdge R760"}
    run.save(update_fields=["listing_proposal"])
    services.create_listings_from_run(run, by=reviewer)

    system = System.objects.get(name="PowerEdge R760")
    assert services.effective_level(run, system) == ValidationLevel.COMMUNITY


# --- CPU: model logged, family derived ----------------------------------------


def test_the_detected_cpu_model_is_prefilled(submitter):
    run = _run(submitter)
    assert run.cpu_model == "Intel(R) Xeon(R) Gold 6430"
    assert RunListingProposalForm.initial_from_run(run)["cpu_model"] == run.cpu_model


def test_a_stored_blank_does_not_hide_the_detected_model(client, submitter):
    """Reported as "why is the CPU model field empty still".

    An empty ``cpu_model`` in the stored proposal shadowed the DMI prefill, and once stored it
    was sticky: every later load rendered empty, and the only way back to the detected value
    was to know it and retype it.

    Any save that omits the field stores that blank - an API client, a hand-made request, or a
    submitter clearing the box. On the reported run it was a verification script of mine
    posting only the identity fields.

    Cosmetic in its effect on the catalog, since ``submitted_cpu_model`` falls back to the run,
    and that is exactly what made it wrong: the form showed nothing where approval would have
    recorded the reported part.
    """
    from lumina.results.views import _proposal_initial

    run = _run(submitter)
    run.listing_proposal = {"cpu_model": "", "name": "PowerEdge R760"}
    run.save(update_fields=["listing_proposal"])

    assert _proposal_initial(run)["cpu_model"] == run.cpu_model
    assert services.submitted_cpu_model(run) == run.cpu_model, "the consumer always agreed"

    client.force_login(submitter)
    body = client.get(reverse("results:propose_listing", args=[run.uuid])).content.decode()
    assert run.cpu_model in body


def test_a_real_correction_still_wins(submitter):
    """The field exists because lscpu reports strings vendors never meant as product names, so
    a non-blank answer must beat the prefill."""
    from lumina.results.views import _proposal_initial

    run = _run(submitter)
    run.listing_proposal = {"cpu_model": "Intel Xeon Gold 6430"}
    run.save(update_fields=["listing_proposal"])

    assert _proposal_initial(run)["cpu_model"] == "Intel Xeon Gold 6430"


def test_an_unticked_release_stays_unticked(submitter):
    """``False`` and ``0`` are answers, not blanks: a major with no evidence and no claim stays
    off, and skipping stored falsy values as though they were empty would tick it.

    Deliberately a major this run says nothing about. The first version of this test used the
    run's *own* release, which the release union now re-ticks - correctly, since the run passed
    on it - so the old assertion was asserting a contradiction rather than the rule.
    """
    from lumina.results.views import _proposal_initial

    run = _run(submitter)
    assert run.alma_release.major != 8, "8 must be a major this run does not prove"
    run.listing_proposal = {"release_8": False}
    run.save(update_fields=["listing_proposal"])

    initial = _proposal_initial(run)
    assert initial["release_8"] is False


def test_the_runs_own_release_is_ticked_even_if_a_stored_blob_says_otherwise(submitter):
    """Reported as the AlmaLinux boxes no longer being checked by default: a run that passed on
    9.8 rendered the 9 box unticked, because a stored ``release_9: False`` sat on top of the
    prefill and ``claimed_release_ticks`` reads the listing and the siblings, deliberately not
    the run in hand.

    An unticked box is not a retraction - ``merge_listing_proposal`` refuses to let a submitter
    take a release back at all - so it must not be able to hide the evidence either.
    """
    from lumina.results.views import _proposal_initial

    run = _run(submitter)
    major = run.alma_release.major
    run.listing_proposal = {f"release_{major}": False}
    run.save(update_fields=["listing_proposal"])

    initial = _proposal_initial(run)

    assert initial[f"release_{major}"] is True
    # No minor travels with it. The tick is the claim; which minor the run passed on is on the
    # run, and a stored floor left behind by an unticked box can no longer widen anything.
    assert not any(key.startswith("release_minor") for key in initial)


def test_no_processor_fields_when_a_model_was_detected(submitter):
    """A detected CPU is tied and corrected in the components section, so neither the top-of-page
    model box nor the family picker is offered - the family is derived from the model anyway, so a
    picker beside it could only contradict it."""
    run = _run(submitter)
    form = RunListingProposalForm(run=run, user=submitter)
    assert "cpu_model" not in form.fields
    assert "cpu_family" not in form.fields


def test_the_family_picker_appears_when_no_model_was_detected(submitter):
    inventory = f.default_inventory()
    inventory["summary"]["cpus"] = [{"model": "", "vendor": "GenuineIntel"}]
    run = _run(submitter, inventory=inventory)

    form = RunListingProposalForm(run=run, user=submitter)
    assert "cpu_family" in form.fields
    labels = [label for _, label in form.fields["cpu_family"].choices]
    assert any("Xeon Scalable 4th Generation" in label for label in labels)


def test_a_corrected_cpu_model_is_what_gets_cataloged(submitter, reviewer):
    """lscpu strings are not always the product name."""
    run = _run(submitter)
    run.listing_proposal = {"vendor_name": "Dell Inc.", "name": "PowerEdge R760",
                            "cpu_model": "Xeon Gold 6430"}
    run.save(update_fields=["listing_proposal"])

    assert services.submitted_cpu_model(run) == "Xeon Gold 6430"


def test_a_hand_picked_family_is_tied_when_there_is_no_model(submitter, reviewer):
    family = Component.objects.get(
        name="Intel Xeon Scalable 4th Generation",
        kind=ComponentKind.cpu.value, role=ComponentRole.FAMILY,
    )
    inventory = f.default_inventory()
    inventory["summary"]["cpus"] = [{"model": "", "vendor": "GenuineIntel"}]
    run = _run(submitter, inventory=inventory)
    run.listing_proposal = {"vendor_name": "Dell Inc.", "name": "PowerEdge R760",
                            "cpu_family": str(family.pk)}
    run.save(update_fields=["listing_proposal"])
    services.create_listings_from_run(run, by=reviewer)
    services.approve_run(release(run), by=reviewer)

    system = System.objects.get(name="PowerEdge R760")
    assert list(system.cpus.all()) == [family]


def test_a_family_pk_that_is_not_a_family_is_ignored(submitter):
    """The picker only lists families, so a model pk here means tampering."""
    model = Component.objects.create(
        vendor=Vendor.objects.get_or_create(
            name="Intel", defaults={"slug": "intel"})[0],
        name="Xeon Gold 6430", kind=ComponentKind.cpu.value,
        role=ComponentRole.MODEL, slug="intel-xeon-gold-6430",
    )
    run = _run(submitter)
    run.listing_proposal = {"cpu_family": str(model.pk)}
    run.save(update_fields=["listing_proposal"])

    assert services.submitted_cpu_family(run) is None


# --- the form as a review surface ----------------------------------------------


def test_every_reported_field_is_editable(submitter):
    """DMI is often wrong, and a reviewer approves the result anyway, so the
    submitter correcting their own hardware costs nothing."""
    run = _run(submitter)
    form = RunListingProposalForm(run=run, user=submitter)
    # cpu_model is not here for a detected CPU: it is corrected in the components section.
    for field in ("vendor_name", "name", "model_number", "description",
                  "vendor_spec_url"):
        assert field in form.fields, field


def test_the_listing_maintenance_fields_are_offered_when_creating_a_listing(submitter):
    """``description`` and ``vendor_spec_url`` belong to whoever maintains the listing.
    Creating one makes that the submitter; see
    ``test_listing_details_gate.py`` for who keeps them on a re-validation."""
    run = _run(submitter)
    form = RunListingProposalForm(run=run, user=submitter)

    assert "description" in form.fields
    assert "vendor_spec_url" in form.fields


def test_the_level_picker_is_hidden_from_community_submitters(submitter):
    """One option is not a choice; showing a dropdown implies otherwise."""
    run = _run(submitter)
    form = RunListingProposalForm(run=run, user=submitter)
    assert "claimed_validation_level" not in form.fields


def test_a_sig_member_can_pick_almalinux_on_the_form(submitter):
    """Myself or AlmaLinux, in one list.

    This asserted on ``claimed_validation_level``, which no longer exists as a rendered
    field: the tier dropdown and the "on behalf of" dropdown were one question asked twice,
    and naming a vendor silently overrode whatever the tier said. They are now a single
    ``attribution`` list.
    """
    group, _ = Group.objects.get_or_create(name="certifier")
    submitter.groups.add(group)
    run = _run(submitter)

    form = RunListingProposalForm(run=run, user=submitter)
    values = [value for value, _ in form.fields["attribution"].choices]
    assert values == [ValidationLevel.COMMUNITY, ValidationLevel.ALMALINUX]


def test_the_form_never_offers_the_vendor_tier(submitter, dell):
    """Not even to a vendor member who could legitimately claim it."""
    VendorMembership.objects.create(
        user=submitter, vendor=dell, role=VendorMembership.ROLE_OWNER
    )
    run = _run(submitter)

    form = RunListingProposalForm(run=run, user=submitter)
    values = [
        value for value, _ in
        form.fields.get("claimed_validation_level").choices
    ] if "claimed_validation_level" in form.fields else []

    assert ValidationLevel.VENDOR not in values


def test_posting_the_form_stores_vendor_and_level_on_the_run(client, submitter,
                                                             dell):
    VendorMembership.objects.create(
        user=submitter, vendor=dell, role=VendorMembership.ROLE_OWNER
    )
    run = _run(submitter)
    client.force_login(submitter)

    client.post(reverse("results:propose_listing", args=[run.uuid]), {
        "vendor_name": "Dell Inc.", "name": "PowerEdge R760",
        "model_number": "", "description": "", "vendor_spec_url": "",
        # One answer now. The tier follows from it rather than being posted alongside.
        "attribution": f"{RunListingProposalForm.VENDOR_CHOICE_PREFIX}{dell.slug}",
    })

    run.refresh_from_db()
    assert run.on_behalf_of == dell
    assert run.claimed_validation_level == ValidationLevel.VENDOR
    # and these do not pollute the listing proposal blob
    assert "on_behalf_of" not in run.listing_proposal
    assert "attribution" not in run.listing_proposal


def test_an_overclaimed_level_is_capped_not_honored(submitter, reviewer):
    """A community submitter posting almalinux gets community anyway."""
    run = _run(submitter)
    run.claimed_validation_level = ValidationLevel.ALMALINUX
    run.listing_proposal = {"vendor_name": "Dell Inc.", "name": "PowerEdge R760"}
    run.save(update_fields=["claimed_validation_level", "listing_proposal"])
    services.create_listings_from_run(run, by=reviewer)

    system = System.objects.get(name="PowerEdge R760")
    assert services.effective_level(run, system) == ValidationLevel.COMMUNITY
