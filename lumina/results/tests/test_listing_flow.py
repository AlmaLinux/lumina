"""The validation-run -> Systems-page path.

The whole point of a validation run is the catalog entry that comes out of
it. These tests cover: auto-linking re-validations of known hardware (with
vendor-name fuzziness), the submitter's propose-a-listing flow, approval
consuming the proposal, and staying quiet when the system is already listed
unless the submitter maintains it.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.urls import reverse

from lumina.hardware.models import System
from lumina.results import ingest, services
from lumina.results.forms import RunListingProposalForm
from lumina.results.models import TestRun
from lumina.results.tests import factories as f
from lumina.results.tests.helpers import release
from lumina.vendors.models import Vendor, VendorMembership

pytestmark = pytest.mark.django_db





@pytest.fixture
def submitter():
    return User.objects.create_user("runner", email="r@example.com")


@pytest.fixture
def reviewer():
    from django.contrib.auth.models import Group

    user = User.objects.create_user("rev", email="rev@example.com")
    group, _ = Group.objects.get_or_create(name="reviewer")
    user.groups.add(group)
    return user


def _validate_run(submitter, run_id=None, version_id="9.6", **report_kw):
    report = f.make_report(
        run_types=["validate"],
        run_id=run_id,
        version_id=version_id,
        results=[f.validate_result("validate.cpu.functional")],
        **report_kw,
    )
    return ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(f.build_bundle(report)),
        source="api",
    )


# --- auto-linking already-cataloged systems ----------------------------------


def test_ingest_auto_links_existing_system(submitter):
    dell = Vendor.objects.create(name="Dell Inc.")
    existing = System.objects.create(vendor=dell, name="PowerEdge R760")

    run = _validate_run(submitter)

    assert run.listing_system == existing


def test_auto_link_handles_vendor_name_variants(submitter):
    """Catalog says "Dell"; DMI says "Dell Inc.". Same vendor, same system."""
    dell = Vendor.objects.create(name="Dell")
    existing = System.objects.create(vendor=dell, name="PowerEdge R760")

    run = _validate_run(submitter)

    assert run.listing_system == existing


def test_auto_linked_run_attests_on_approval(submitter, reviewer):
    dell = Vendor.objects.create(name="Dell Inc.")
    existing = System.objects.create(vendor=dell, name="PowerEdge R760")

    run = _validate_run(submitter)
    services.approve_run(release(run), by=reviewer)

    existing.refresh_from_db()
    assert existing.attestation_count == 1
    assert existing.published is True


def test_no_auto_link_for_unknown_model(submitter):
    run = _validate_run(submitter)
    assert run.listing_system is None


# --- the submitter propose flow ----------------------------------------------


def test_submitter_sees_propose_prompt_for_new_model(client, submitter):
    run = _validate_run(submitter)
    client.force_login(submitter)
    resp = client.get(run.get_absolute_url())
    # One control, in the draft alert: the prompt below explains, it does not
    # offer a second button to the same form.
    assert "is not in the catalog yet" in resp.text
    assert resp.text.count("propose-listing/") == 1
    assert reverse("results:propose_listing", args=[run.uuid]) in resp.text


def test_other_users_see_no_prompt(client, submitter):
    run = _validate_run(submitter)
    other = User.objects.create_user("someone")
    client.force_login(other)
    # another user cannot see a draft run at all; publish it first
    services.approve_run(release(run), by=other)
    resp = client.get(run.get_absolute_url())
    assert "List this system" not in resp.text


def test_propose_form_prefills_from_run_and_resolves_vendor(client, submitter):
    Vendor.objects.create(name="Dell")  # catalog spelling differs from DMI
    run = _validate_run(submitter)
    # DMI in this fixture matches an existing System only if one exists;
    # here only the vendor exists, so the prompt is still "propose"
    client.force_login(submitter)
    resp = client.get(reverse("results:propose_listing", args=[run.uuid]))
    assert resp.status_code == 200
    # vendor field prefilled with the *catalog* spelling, not the DMI string
    assert 'value="Dell"' in resp.text
    assert 'value="PowerEdge R760"' in resp.text


def test_proposal_saved_and_consumed_on_approval(client, submitter, reviewer):
    run = _validate_run(submitter)
    client.force_login(submitter)
    resp = client.post(
        reverse("results:propose_listing", args=[run.uuid]),
        {
            "vendor_name": "Dell Inc.",
            "name": "PowerEdge R760",
            "model_number": "R760XS",
        },
    )
    assert resp.status_code == 302
    run.refresh_from_db()
    assert run.listing_proposal["model_number"] == "R760XS"

    services.approve_run(release(run), by=reviewer)

    run.refresh_from_db()
    system = run.listing_system
    assert system is not None
    assert system.name == "PowerEdge R760"
    assert system.model_number == "R760XS"
    # No description or spec URL: those fields left this form. A new listing starts
    # without them and its owner fills them in through hardware:propose_edit.
    assert system.description == ""
    assert system.vendor_spec_url == ""
    assert system.created_by == submitter
    assert system.published is True
    assert system.attestation_count == 1


def test_proposal_reuses_existing_vendor_via_alias_resolution(
    client, submitter, reviewer
):
    dell = Vendor.objects.create(name="Dell")
    run = _validate_run(submitter)
    client.force_login(submitter)
    client.post(
        reverse("results:propose_listing", args=[run.uuid]),
        {"vendor_name": "Dell Inc.", "name": "PowerEdge R760"},
    )
    run.refresh_from_db()
    services.approve_run(release(run), by=reviewer)
    run.refresh_from_db()
    assert run.listing_system.vendor == dell
    # no "Dell Inc." duplicate created (the CPU tie adds its own CPU vendor)
    assert Vendor.objects.filter(name__icontains="dell").count() == 1


# --- already-listed systems ---------------------------------------------------


def test_no_prompt_when_system_already_listed(client, submitter):
    dell = Vendor.objects.create(name="Dell Inc.")
    System.objects.create(vendor=dell, name="PowerEdge R760")
    run = _validate_run(submitter)  # auto-linked at ingest
    client.force_login(submitter)
    resp = client.get(run.get_absolute_url())
    assert "List this system" not in resp.text
    assert "Propose changes" not in resp.text


def test_the_owner_is_told_nothing_is_needed_without_an_edit_banner(client, submitter):
    """Already-cataloged hardware the submitter's vendor maintains.

    This asserted the opposite until the banner came out: a top-of-page alert reading
    "This model is already listed as X, which your vendor maintains" with a Propose
    changes button. It said what the page says plainly further down - the run is linked,
    and the Linked system row names that listing and links to it - because
    ``auto_link_existing_system`` runs at ingest, so the listing the banner named *was*
    ``run.listing_system``.

    What has to survive is the part that is not repeated anywhere: the draft alert
    telling the submitter no listing details are wanted from them. Without it they are
    asked to describe hardware the catalog already has.
    """
    dell = Vendor.objects.create(name="Dell Inc.", verified=True)
    listing = System.objects.create(
        vendor=dell, name="PowerEdge R760", owner_vendor=dell,
    )
    VendorMembership.objects.create(
        user=submitter, vendor=dell, role=VendorMembership.ROLE_SUBMITTER
    )
    run = _validate_run(submitter)
    client.force_login(submitter)

    body = client.get(run.get_absolute_url()).text

    assert "Propose changes" not in body
    assert "This model is already listed as" not in body
    assert "List this system" not in body
    # Still says the catalog has it, and still points at it.
    assert "already in the catalog" in body
    assert listing.name in body


def test_the_linked_listing_is_still_reachable_from_the_run(client, submitter):
    """The banner carried the only link on some paths, so this pins the one that
    replaces it rather than trusting that a link exists somewhere on the page."""
    dell = Vendor.objects.create(name="Dell Inc.", verified=True)
    listing = System.objects.create(
        vendor=dell, name="PowerEdge R760", owner_vendor=dell, published=True,
    )
    VendorMembership.objects.create(
        user=submitter, vendor=dell, role=VendorMembership.ROLE_SUBMITTER
    )
    run = _validate_run(submitter)
    run.refresh_from_db()
    assert run.listing_system == listing, "ingest should have auto-linked this"
    client.force_login(submitter)

    body = client.get(run.get_absolute_url()).text

    assert reverse("hardware:detail", args=[listing.slug]) in body


def test_the_identity_is_locked_on_hardware_the_submitter_does_not_speak_for(
    client, submitter
):
    """A re-validation is evidence *about* a listing, not an occasion to restate what the
    listing is.

    This assertion has now been wrong in three different ways, which is worth recording. It
    first bounced with "already in the catalog" for everyone, refusing the vendor too. Then it
    opened for everyone, letting any community member post the identity of a manufacturer's own
    listing. Then it refused the page to non-vendors, which shut out a component vendor
    claiming their own part and a submitter whose run was misidentified.

    The rule is the same throughout and only its enforcement point moved: the page is open, the
    identity fields are locked, and ``clean`` discards what a locked reader posts. Details in
    ``test_listing_details_gate.py``.
    """
    dell = Vendor.objects.create(name="Dell Inc.")
    System.objects.create(vendor=dell, name="PowerEdge R760")
    run = _validate_run(submitter)
    client.force_login(submitter)

    resp = client.get(reverse("results:propose_listing", args=[run.uuid]))

    assert resp.status_code == 200
    assert RunListingProposalForm(
        run=run, user=submitter, subject="system",
    ).identity_locked is True


def test_the_vendor_may_still_detail_their_own_listing(client, submitter):
    """The half the old blanket gate got wrong. A Dell engineer re-running the suite on a
    Dell machine is exactly who should be able to correct its catalog entry.

    Membership in the listing's *manufacturer* counts, not only in its ``owner_vendor``:
    a listing the community catalogued first has no owner at all, and Dell would fail
    ``can_edit_listing`` on their own hardware.
    """
    dell = Vendor.objects.create(name="Dell Inc.", verified=True)
    System.objects.create(vendor=dell, name="PowerEdge R760")
    VendorMembership.objects.create(
        user=submitter, vendor=dell, role=VendorMembership.ROLE_SUBMITTER,
    )
    run = _validate_run(submitter)
    client.force_login(submitter)

    resp = client.get(reverse("results:propose_listing", args=[run.uuid]))

    assert resp.status_code == 200


def test_the_button_is_offered_on_a_revalidation(client, submitter):
    """The entry point. This asserted the opposite - that the button was hidden on a run
    against known hardware - and hiding it is what made the misidentification override
    unreachable, since the run whose match is wrong is exactly the one that looks like a
    plain re-validation.

    The releases validated, the parts to tie, and the submitter's own notes are all on that
    form and all theirs. What they may not do is restate the machine, which is enforced on
    the fields.
    """
    dell = Vendor.objects.create(name="Dell Inc.")
    System.objects.create(vendor=dell, name="PowerEdge R760")
    run = _validate_run(submitter)
    client.force_login(submitter)

    body = client.get(run.get_absolute_url()).text

    assert reverse("results:propose_listing", args=[run.uuid]) in body


def test_the_form_is_closed_once_the_run_is_under_review(client, submitter):
    run = _validate_run(submitter)
    run.status = TestRun.STATUS_PENDING
    run.save(update_fields=["status"])
    client.force_login(submitter)

    resp = client.get(reverse("results:propose_listing", args=[run.uuid]))

    assert resp.status_code == 302


def test_custom_build_is_prompted_about_its_motherboard(client, submitter):
    """Not about a "system": on a custom build the board is the listing. The
    prompt used to be skipped entirely for these."""
    run = _validate_run(submitter, inventory=f.custom_build_inventory())
    client.force_login(submitter)

    resp = client.get(run.get_absolute_url())

    assert "The motherboard" in resp.text
    assert "Add listing details" in resp.text
    assert "already in the catalog" not in resp.text
    # "Edit listing details" and "Edit details" both pointing at one form was
    # the redundancy; a draft page links to it exactly once.
    assert resp.text.count("propose-listing/") == 1
    body = client.get(reverse("results:propose_listing", args=[run.uuid])).text
    # Labeled as the displayed, human-friendly name, distinct from a part code.
    assert "Displayed motherboard name" in body


def test_vendor_rename_in_proposal_learns_an_alias(client, submitter, reviewer):
    """DMI said "Dell"; the submitter wrote "Dell Technologies". The DMI
    string becomes an alias so the next run of this model auto-links
    without asking anyone."""
    run = _validate_run(submitter)  # DMI vendor: "Dell Inc."
    client.force_login(submitter)
    client.post(
        reverse("results:propose_listing", args=[run.uuid]),
        {"vendor_name": "Dell Technologies", "name": "PowerEdge R760"},
    )
    run.refresh_from_db()
    services.approve_run(release(run), by=reviewer)

    # second run of the same machine: auto-linked at ingest via the alias
    second = _validate_run(
        submitter, run_id="bbbb1111-2222-3333-4444-555566667777"
    )
    assert second.listing_system is not None
    assert second.listing_system.vendor.name == "Dell Technologies"


# --- trust tier shown on certification evidence -------------------------------


def test_attestation_freezes_the_trust_level(submitter, reviewer):
    """A vendor member's run is recorded as vendor-tier evidence."""
    from lumina.hardware.models import CommunityAttestation

    dell = Vendor.objects.create(name="Dell Inc.", verified=True)
    system = System.objects.create(vendor=dell, name="PowerEdge R760",
                                   owner_vendor=dell)
    VendorMembership.objects.create(
        user=submitter, vendor=dell, role=VendorMembership.ROLE_SUBMITTER
    )
    run = _validate_run(submitter)
    run.claimed_validation_level = "vendor"
    run.on_behalf_of = dell      # the tier is tied to attribution, not membership
    run.save()
    services.approve_run(release(run), by=reviewer)

    attestation = CommunityAttestation.objects.get(
        test_run=run, listing_system=system
    )
    assert attestation.level == "vendor"
    assert services.run_trust_level(run, system) == "vendor"


def test_trust_level_defaults_to_community(submitter, reviewer):
    dell = Vendor.objects.create(name="Dell Inc.")
    system = System.objects.create(vendor=dell, name="PowerEdge R760")
    run = _validate_run(submitter)
    services.approve_run(release(run), by=reviewer)
    assert services.run_trust_level(run, system) == "community"


def test_system_page_shows_who_validated(client, submitter, reviewer):
    dell = Vendor.objects.create(name="Dell Inc.")
    system = System.objects.create(vendor=dell, name="PowerEdge R760")
    run = _validate_run(submitter)
    services.approve_run(release(run), by=reviewer)

    resp = client.get(reverse("hardware:detail", args=[system.slug]))
    assert "Validated by" in resp.text
    assert "Community" in resp.text
    assert submitter.username in resp.text


# --- AlmaLinux compatibility from runs ----------------------------------------


@pytest.fixture
def release9():
    from lumina.releases.models import AlmaLinuxRelease

    # get_or_create: conftest's autouse fixture already seeds 8, 9, and 10, and
    # this fixture predates it.
    return AlmaLinuxRelease.objects.get_or_create(major=9)[0]


def _versions_for(listing):
    """The majors a listing records. Certification is per major, so that is the whole row."""
    return {v.release.major for v in listing.versions.all()}


def test_passing_run_records_compatibility_on_system_and_components(
    client, submitter, reviewer, release9
):
    dell, _ = Vendor.objects.get_or_create(name="Dell Inc.")
    system = System.objects.create(vendor=dell, name="PowerEdge R760")
    run = _validate_run(submitter)  # factory reports AlmaLinux 9.6
    services.approve_run(release(run), by=reviewer)

    system.refresh_from_db()
    # A 9.6 pass records AlmaLinux 9. The 9.6 itself stays on the run.
    assert _versions_for(system) == {9}
    cpu = run.listing_components.get(kind="cpu")
    board = run.listing_components.get(kind="motherboard")
    assert _versions_for(cpu) == {9}
    assert _versions_for(board) == {9}
    # and the system is now discoverable through the alma filter
    from lumina.hardware.filters import filter_listings

    assert system in filter_listings(System, params={"alma": ["9"]})


def test_two_runs_on_the_same_major_stay_one_row(submitter, reviewer, release9):
    """Two passes on different minors of the same major are one claim, and one row.

    These were two tests about the ``minimum_minor`` floor - a 9.2 run extended it down from
    9.6, and a 9.8 run never raised it. Certification is per major now, so the pair collapses
    into the invariant that survived: one row per (listing, major) however many runs land on it.
    """
    dell, _ = Vendor.objects.get_or_create(name="Dell Inc.")
    system = System.objects.create(vendor=dell, name="PowerEdge R760")
    first = _validate_run(submitter, version_id="9.6")
    services.approve_run(release(first), by=reviewer)
    second = _validate_run(
        submitter, version_id="9.2",
        run_id="ffff1111-2222-3333-4444-555566667777",
    )
    services.approve_run(release(second), by=reviewer)

    system.refresh_from_db()
    assert _versions_for(system) == {9}
    assert system.versions.count() == 1


def test_failing_run_records_no_compatibility(submitter, reviewer, release9):
    dell, _ = Vendor.objects.get_or_create(name="Dell Inc.")
    system = System.objects.create(vendor=dell, name="PowerEdge R760")
    report = f.make_report(
        run_types=["validate"],
        results=[f.validate_result("validate.cpu.functional", status="fail")],
    )
    run = ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(f.build_bundle(report)),
        source="api",
    )
    run.listing_system = system
    run.save()
    services.approve_run(release(run), by=reviewer)
    assert _versions_for(system) == set()


def test_unknown_release_records_nothing(submitter, reviewer):
    """No AlmaLinuxRelease row for the reported version -> skip, not crash."""
    dell, _ = Vendor.objects.get_or_create(name="Dell Inc.")
    System.objects.create(vendor=dell, name="PowerEdge R760")
    run = _validate_run(submitter, version_id="42.0")
    services.approve_run(release(run), by=reviewer)
    assert services.record_compatibility(run) == []


# --- machine-type codes ------------------------------------------------------


def _lenovo_inventory():
    """Lenovo reports a readable model and a separate machine-type code.

    The suite resolves DMI type 1 Version into `product` and keeps Product
    Name as `model_number`; before that, run 4f47867b listed itself as
    "Custom build: LENOVO 21K9001NUS".
    """
    inventory = f.default_inventory()
    inventory["summary"]["system"].update({
        "vendor": "LENOVO",
        "product": "ThinkBook 14 G6+ ABP",
        "model_number": "21K9001NUS",
        "kind": "prebuilt",
    })
    inventory["summary"]["baseboard"].update({
        "vendor": "LENOVO", "product": "21K9001NUS",
    })
    return inventory


def test_machine_type_code_is_denormalized_from_the_report(submitter):
    run = _validate_run(submitter, inventory=_lenovo_inventory())
    assert run.system_product == "ThinkBook 14 G6+ ABP"
    assert run.system_model_number == "21K9001NUS"


def test_machine_type_code_prefills_the_submitter_form(submitter):
    """The submitter should not have to retype a code DMI already reported."""

    run = _validate_run(submitter, inventory=_lenovo_inventory())
    initial = RunListingProposalForm.initial_from_run(run)
    assert initial["name"] == "ThinkBook 14 G6+ ABP"
    assert initial["model_number"] == "21K9001NUS"


def test_created_listing_carries_the_machine_type_code(submitter, reviewer):
    run = _validate_run(submitter, inventory=_lenovo_inventory())
    run.listing_proposal = {"vendor_name": "Lenovo",
                            "name": "ThinkBook 14 G6+ ABP"}
    run.save(update_fields=["listing_proposal"])

    services.create_listings_from_run(run, by=reviewer)

    system = System.objects.get(name="ThinkBook 14 G6+ ABP")
    # Not in the proposal, so it has to come from the run itself.
    assert system.model_number == "21K9001NUS"


def test_reports_without_a_machine_type_code_are_unaffected(submitter):
    """Dell and HP put the readable model in Product Name; nothing to carry."""
    run = _validate_run(submitter)
    assert run.system_product == "PowerEdge R760"
    assert run.system_model_number == ""


# --- a failing run must not look like a success -------------------------------


def _failing_run(submitter):
    """A run blocked by one non-informational failure.

    Run 9aac4289 hit this: a required test errored, so approval certified
    nothing while the UI still said "Approved and published."
    """
    report = f.make_report(
        run_types=["validate"],
        results=[
            f.validate_result("validate.cpu.functional"),
            f.validate_result("validate.storage.smart", status="error",
                              severity="required",
                              reason="could not reach the registry"),
        ],
        inventory=_lenovo_inventory(),
    )
    return ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(f.build_bundle(report)),
        source="api",
    )


def test_failing_run_certifies_nothing_on_approval(submitter, reviewer):
    run = _failing_run(submitter)
    run.listing_proposal = {"vendor_name": "Lenovo", "name": "ThinkPad P16s Gen 2"}
    run.save(update_fields=["listing_proposal"])
    services.create_listings_from_run(run, by=reviewer)
    services.approve_run(release(run), by=reviewer)

    assert run.verdict() is False
    system = System.objects.get(name="ThinkPad P16s Gen 2")
    assert system.published is False          # never reaches the catalog
    assert system.attestation_count == 0
    assert list(run.listing_components.all()) == []   # no component ties either
    assert list(system.cpus.all()) == []


def test_review_page_warns_that_a_failing_run_certifies_nothing(client, submitter,
                                                                reviewer):
    run = _failing_run(submitter)
    client.force_login(reviewer)

    body = client.get(reverse("review:run_detail", args=[run.pk])).content.decode()

    assert "did not pass" in body
    assert "not</strong> certify" in body
    # and it names the offender rather than leaving the reviewer to hunt
    assert "validate.storage.smart" in body


def test_approving_a_failing_run_does_not_claim_success(client, submitter, reviewer):
    run = release(_failing_run(submitter))
    client.force_login(reviewer)

    response = client.post(reverse("review:run_approve", args=[run.pk]), follow=True)

    text = " ".join(str(m) for m in response.context["messages"])
    assert "did not pass" in text
    assert "no listing was certified" in text


def test_passing_run_still_reports_plain_success(client, submitter, reviewer):
    run = release(_validate_run(submitter))
    client.force_login(reviewer)

    response = client.post(reverse("review:run_approve", args=[run.pk]), follow=True)

    text = " ".join(str(m) for m in response.context["messages"])
    assert text == "Approved and published."
