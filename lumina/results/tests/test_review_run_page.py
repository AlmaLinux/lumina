"""What the reviewer's run page says approving will do, and how many boxes it takes to say it.

Three reports, one page:

1. "The 'proposed catalog listing' box is broken." It was, on any run against hardware already
   in the catalog. It printed the proposal blob's ``vendor_name`` and ``name`` under copy
   promising that approving "creates the System listing with these details" - and on such a run
   neither key is in the blob, because the identity is locked for anybody who does not speak for
   the listing's vendor and the posted values are discarded. So it rendered two labels with
   nothing after them, above a sentence that was wrong anyway: approving reuses the listing.

2. "Couldn't this be combined into fewer boxes, like the submitter's side?" It could. The page
   had "Proposed catalog listing" beside "Assign a listing" describing the same decision from
   opposite ends, and "Will be attached on approval" beside "Adjust before approving" rendering
   the same component entries twice with different information beside each copy.

3. "In the summary block we should list the kernel." It was collected and stored all along and
   surfaced nowhere.

The through-line is that a reviewer should be told what approving does, once, from what the code
will actually use. ``proposal_effect`` derives it the way ``create_listings_from_run`` does.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import Group, User
from django.urls import reverse

from lumina.hardware.models import System
from lumina.releases.models import AlmaLinuxRelease
from lumina.results import ingest, services
from lumina.results.tests import factories as f
from lumina.results.tests.helpers import release as _ready
from lumina.vendors.models import Vendor, VendorMembership

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def releases():
    for major in (9, 10):
        AlmaLinuxRelease.objects.get_or_create(major=major, defaults={"supported": True})


@pytest.fixture
def submitter():
    return User.objects.create_user("rrp-sub", password="pw")


@pytest.fixture
def reviewer():
    user = User.objects.create_user("rrp-rev", password="pw")
    group, _ = Group.objects.get_or_create(name="reviewer")
    user.groups.add(group)
    return user


def _run(submitter, **report_kw):
    return ingest.ingest_bundle(
        submitter=submitter, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"],
            results=[f.validate_result("validate.cpu.functional")],
            **report_kw,
        ))),
    )


def _listed(submitter):
    """A run whose machine the catalog already holds - the reported-broken case."""
    dell, _ = Vendor.objects.get_or_create(name="Dell Inc.", defaults={"published": True})
    listing = System.objects.create(vendor=dell, name="PowerEdge R760")
    run = _run(submitter)
    assert services.existing_listing_for(run) == listing, "the fixture must start matched"
    return run, listing


def _body(client, reviewer, run):
    client.force_login(reviewer)
    return client.get(reverse("review:run_detail", args=[run.pk])).content.decode()


def _prose(client, reviewer, run):
    """The page with whitespace collapsed, for assertions on sentences.

    A template wraps its copy wherever the line ran out, so a phrase that reads as one string on
    the page is "cannot add a\n release..." in the body. Structural assertions keep using
    ``_body``; only prose uses this.
    """
    return " ".join(_body(client, reviewer, run).split())


# --- what approving will do ------------------------------------------------------


def test_a_matched_run_says_it_attests_rather_than_creates(submitter):
    run, listing = _listed(submitter)

    effect = services.proposal_effect(run)

    assert effect["creates"] is False
    assert effect["listing"] == listing


def test_a_new_machine_says_it_creates(submitter):
    run = _run(submitter)
    assert services.existing_listing_for(run) is None, "the premise"

    effect = services.proposal_effect(run)

    assert effect["creates"] is True
    assert effect["listing"] is None


def test_the_identity_falls_back_to_the_report(submitter):
    """The bug in one assertion. The blob carries no identity for this run, and the box printed
    the blob - so it printed nothing. Approving uses the reported strings, so that is what a
    reviewer has to be shown."""
    run = _run(submitter)
    run.listing_proposal = {"release_9": True}
    run.save(update_fields=["listing_proposal"])

    effect = services.proposal_effect(run)

    assert effect["vendor"] == run.system_vendor
    assert effect["name"] == run.system_product
    assert effect["from_report"] is True


def test_a_submitters_own_answer_wins(submitter):
    run = _run(submitter)
    run.listing_proposal = {"vendor_name": "Supermicro", "name": "Whitebox 1U"}
    run.save(update_fields=["listing_proposal"])

    effect = services.proposal_effect(run)

    assert (effect["vendor"], effect["name"]) == ("Supermicro", "Whitebox 1U")
    assert effect["from_report"] is False


def test_it_agrees_with_what_approval_actually_creates(submitter, reviewer):
    """The promise the box makes has to be the one approval keeps. Both read the proposal-then-
    report fallback, and this is what stops them drifting apart."""
    run = _run(submitter)
    run.listing_proposal = {"vendor_name": "Supermicro", "name": "Whitebox 1U"}
    run.save(update_fields=["listing_proposal"])
    effect = services.proposal_effect(run)

    services.create_listings_from_run(run, by=reviewer)

    run.refresh_from_db()
    assert run.listing_system.name == effect["name"]
    assert run.listing_system.vendor.name == effect["vendor"]


def _declare(listing, major, level=""):
    """Give ``listing`` a release row it already holds, the way an earlier run would have."""
    from lumina.hardware.models import ListingVersion
    from lumina.releases.models import AlmaLinuxRelease

    release, _ = AlmaLinuxRelease.objects.get_or_create(
        major=major, defaults={"supported": True},
    )
    return ListingVersion.objects.create(
        listing_system=listing, release=release,
        source=ListingVersion.SOURCE_RUN, validation_level=level,
    )


def test_support_the_listing_already_has_is_reported_as_unchanged(submitter):
    """Reported: "'Also ticked' implies 'we' ticked it. It was already set and we're simply
    carrying it forward. It's existing implied support that we are not modifying in any way."

    Exactly so, and the page said the opposite twice: it credited the tick to the submitter and
    then warned it would not be recorded, which reads as a loss when there is nothing to record.
    ``claimed_release_ticks`` pre-ticks whatever the listing already claims, so that saving the
    form does not look like a retraction - which makes the tick a carry-forward, not a claim.
    """
    run, listing = _listed(submitter)
    _declare(listing, 10, level="community")
    run.listing_proposal = {"release_10": True}
    run.save(update_fields=["listing_proposal"])

    effect = services.proposal_effect(run)

    assert effect["carried"] == [{
        "major": 10, "level": "community", "level_display": "Community-validated",
    }]
    assert effect["new_declarations"] == [], "nothing here is new"


def test_the_listings_own_tier_is_what_is_shown(submitter):
    """The row is about the catalog, so it reports what the catalog holds for that major.

    It used to report the listing's minor floor rather than the tick's, for the same reason -
    they could differ. With majors only, the tier is the thing that can.
    """
    run, listing = _listed(submitter)
    _declare(listing, 10, level="almalinux")
    run.listing_proposal = {"release_10": True}
    run.save(update_fields=["listing_proposal"])

    assert services.proposal_effect(run)["carried"][0]["level"] == "almalinux"


def test_a_major_the_listing_lacks_is_a_real_declaration(submitter):
    """The one case where the tick *is* the submitter's own claim - and the case where approving a
    re-validation drops it."""
    run, _ = _listed(submitter)
    run.listing_proposal = {"release_10": True}
    run.save(update_fields=["listing_proposal"])

    effect = services.proposal_effect(run)

    assert effect["carried"] == []
    assert effect["new_declarations"] == [{"major": 10}]
    assert effect["declares"] is False, "an existing listing records none of them"


def test_a_new_listing_does_record_the_declarations(submitter):
    run = _run(submitter)
    run.listing_proposal = {"release_10": True}
    run.save(update_fields=["listing_proposal"])

    effect = services.proposal_effect(run)

    assert effect["declares"] is True
    assert effect["new_declarations"] == [{"major": 10}]


def test_the_run_own_release_appears_in_neither_list(submitter):
    """It is evidence, reported as ``run_release``. Repeating it below is what made the original
    single row ambiguous."""
    run, listing = _listed(submitter)
    major = run.alma_release.major
    _declare(listing, major, level="community")
    run.listing_proposal = {f"release_{major}": True}
    run.save(update_fields=["listing_proposal"])

    effect = services.proposal_effect(run)

    assert effect["carried"] == []
    assert effect["new_declarations"] == []


def test_an_unticked_release_is_not_a_claim(submitter):
    """``_claimed_majors`` reads a side's minor only where that side claims the major, so an
    unticked box with a floor of 0 beside it is not reported as "8.0+"."""
    run, _ = _listed(submitter)
    run.listing_proposal = {"release_8": False}
    run.save(update_fields=["listing_proposal"])

    effect = services.proposal_effect(run)

    assert effect["carried"] == []
    assert effect["new_declarations"] == []


def test_details_are_marked_as_not_applying(submitter):
    """A description from somebody who does not speak for the listing's vendor never reaches it
    (``apply_owner_maintenance``). Saying so beats a reviewer wondering why text they can see is
    not going to appear."""
    run, _ = _listed(submitter)

    assert services.proposal_effect(run)["applies_details"] is False


def test_details_do_apply_for_the_listings_own_vendor(submitter):
    run, listing = _listed(submitter)
    VendorMembership.objects.create(
        user=submitter, vendor=listing.vendor, role=VendorMembership.ROLE_SUBMITTER,
    )

    assert services.proposal_effect(run)["applies_details"] is True


# --- the page ---------------------------------------------------------------------


def test_the_page_no_longer_shows_empty_identity_rows(client, submitter, reviewer):
    """The reported symptom: "Vendor" and "Name" as labels with nothing after them."""
    run, _ = _listed(submitter)
    run.listing_proposal = {"release_9": True}
    run.save(update_fields=["listing_proposal"])

    body = _body(client, reviewer, run)

    assert "Approving attests an existing listing" in body
    assert "creates the System listing with these details" not in body
    assert "<dt class=\"col-sm-4\">Vendor</dt>" not in body, (
        "a run that creates nothing has no proposed identity to show"
    )


def test_the_incoming_tier_is_reported(submitter):
    """Asked for: "for 'Proves' why don't we say the validation tier it is coming in with?" The
    row named the release and left the tier to be inferred from the dropdown below, which is a
    ceiling rather than the answer."""
    run, listing = _listed(submitter)

    effect = services.proposal_effect(run)

    assert effect["level"] == services.run_trust_level(run, listing)
    assert effect["level_display"] == "Community-validated"


def test_the_tier_shown_is_the_capped_one_not_the_claim(submitter):
    """The guarantee the row must not overstate: a run can never grant more trust than its
    submitter has *for that listing*.

    Here the submitter speaks for Intel and claims the vendor tier on a Dell machine. The claim
    says vendor; the evidence is worth community. Added because a mutation that read the tier
    straight off ``run.claimed_validation_level`` passed every other test in this file - in all of
    them the claim and the derivation happen to agree.
    """
    intel, _ = Vendor.objects.get_or_create(name="Intel", defaults={"published": True})
    intel.verified = True
    intel.save(update_fields=["verified"])
    run, _ = _listed(submitter)
    VendorMembership.objects.create(
        user=submitter, vendor=intel, role=VendorMembership.ROLE_SUBMITTER,
    )
    run.on_behalf_of = intel
    run.claimed_validation_level = "vendor"
    run.save(update_fields=["on_behalf_of", "claimed_validation_level"])

    effect = services.proposal_effect(run)

    assert run.claimed_validation_level == "vendor", "the claim"
    assert effect["level"] == "community", "what it is actually worth here"
    assert effect["level_display"] == "Community-validated"


def test_the_incoming_tier_matches_what_approval_records(submitter, reviewer):
    """The create path has no listing to ask, so the tier is derived from the vendor the listing
    *will* have - a mirror of ``create_listings_from_run``'s ``owner_vendor`` rule. This is what
    stops the mirror drifting: the number shown before approval has to be the one recorded
    after."""
    dell, _ = Vendor.objects.get_or_create(name="Dell Inc.", defaults={"published": True})
    dell.verified = True
    dell.save(update_fields=["verified"])
    run = _run(submitter)
    VendorMembership.objects.create(
        user=submitter, vendor=dell, role=VendorMembership.ROLE_SUBMITTER,
    )
    run.listing_proposal = {"vendor_name": "Dell Inc.", "name": "PowerEdge R760"}
    run.on_behalf_of = dell
    run.claimed_validation_level = "vendor"
    run.save(update_fields=["listing_proposal", "on_behalf_of", "claimed_validation_level"])
    predicted = services.proposal_effect(run)["level"]

    services.approve_run(_ready(run), by=reviewer)

    run.refresh_from_db()
    version = run.listing_system.versions.get(release=run.alma_release)
    assert predicted == "vendor"
    assert version.validation_level == predicted


def test_a_vendor_claim_for_somebody_elses_machine_is_not_promised(submitter):
    """The mirror's whole point. Attributing to a vendor that does not make the machine being
    created cannot buy a vendor tier, and promising one on the page would be a lie a reviewer
    would only discover after approving."""
    intel, _ = Vendor.objects.get_or_create(name="Intel", defaults={"published": True})
    intel.verified = True
    intel.save(update_fields=["verified"])
    run = _run(submitter)
    VendorMembership.objects.create(
        user=submitter, vendor=intel, role=VendorMembership.ROLE_SUBMITTER,
    )
    run.listing_proposal = {"vendor_name": "Dell Inc.", "name": "PowerEdge R760"}
    run.on_behalf_of = intel
    run.claimed_validation_level = "vendor"
    run.save(update_fields=["listing_proposal", "on_behalf_of", "claimed_validation_level"])

    assert services.proposal_effect(run)["level"] == "community"


def test_parts_capped_lower_are_named(submitter):
    """The other half of "components? only the system?" - the parts that fall back are named
    rather than described."""
    run, listing = _listed(submitter)
    dell = listing.vendor
    dell.verified = True
    dell.save(update_fields=["verified"])
    VendorMembership.objects.create(
        user=submitter, vendor=dell, role=VendorMembership.ROLE_SUBMITTER,
    )
    run.on_behalf_of = dell
    run.claimed_validation_level = "vendor"
    run.save(update_fields=["on_behalf_of", "claimed_validation_level"])
    services.ensure_component_ties(run)

    effect = services.proposal_effect(run)

    assert effect["level"] == "vendor"
    capped = {part["label"] for part in effect["parts_capped"]}
    assert capped, "the fixture has parts Dell did not make"
    assert all("Dell" not in label for label in capped), capped
    assert all(part["level_display"] == "Community-validated"
               for part in effect["parts_capped"])


def test_nothing_is_flagged_as_capped_when_the_tier_is_uniform(submitter):
    """A community run gives every listing the same tier, so there is nothing to warn about and a
    row that always warned would be noise."""
    run, _ = _listed(submitter)
    services.ensure_component_ties(run)

    assert services.proposal_effect(run)["parts_capped"] == []


def test_the_page_shows_the_tier_in_the_proves_row(client, submitter, reviewer):
    run, _ = _listed(submitter)

    body = _prose(client, reviewer, run)

    assert "as <strong>Community-validated</strong>" in body


def test_the_page_names_the_parts_that_are_capped(client, submitter, reviewer):
    run, listing = _listed(submitter)
    dell = listing.vendor
    dell.verified = True
    dell.save(update_fields=["verified"])
    VendorMembership.objects.create(
        user=submitter, vendor=dell, role=VendorMembership.ROLE_SUBMITTER,
    )
    run.on_behalf_of = dell
    run.claimed_validation_level = "vendor"
    run.save(update_fields=["on_behalf_of", "claimed_validation_level"])
    services.ensure_component_ties(run)

    body = _prose(client, reviewer, run)

    assert "Attached parts are capped by their own maker" in body
    assert "gets Community-validated" in body


def test_carried_support_shows_a_label_not_a_slug(client, submitter, reviewer):
    run, listing = _listed(submitter)
    _declare(listing, 10, level="community")
    run.listing_proposal = {"release_10": True}
    run.save(update_fields=["listing_proposal"])

    body = _prose(client, reviewer, run)

    assert "(Community-validated)" in body
    assert "(community)" not in body


def test_the_page_says_which_release_the_tier_lands_on(client, submitter, reviewer):
    """The answer to the reported question, on the page. The attestation hangs off the version
    row for the release the run passed on, so that is the only tier the validation level can
    move."""
    run, _ = _listed(submitter)

    # ``_prose`` because the template wraps "(run on 9.6)" across a line break.
    body = _prose(client, reviewer, run)

    assert "Proves" in body
    assert f"AlmaLinux {run.alma_release.major}" in body
    # The minor is still shown, as provenance for the evidence rather than the scope of it.
    assert f"(run on {run.alma_release.major}.{run.alma_minor})" in body
    assert "this major" in body


def test_the_page_says_existing_support_is_untouched(client, submitter, reviewer):
    """The reported case: the run's listing already records AlmaLinux 10, the form pre-ticked
    it, and the page reported it as the submitter's claim being thrown away."""
    run, listing = _listed(submitter)
    _declare(listing, 10, level="community")
    run.listing_proposal = {"release_10": True}
    run.save(update_fields=["listing_proposal"])

    body = _body(client, reviewer, run)

    assert "Unchanged" in body
    assert "AlmaLinux 10" in body
    assert "carried forward as it stands" in body
    assert "Also ticked" not in body, "it was not ticked by this submitter"
    assert "Claimed but dropped" not in body, "nothing is being dropped"


def test_the_page_still_warns_when_a_claim_really_is_dropped(client, submitter, reviewer):
    """The genuine gap, and it stays visible: a major the catalog does not hold is the submitter's
    own claim, and approving a re-validation does not record it."""
    run, _ = _listed(submitter)
    run.listing_proposal = {"release_10": True}
    run.save(update_fields=["listing_proposal"])

    body = _prose(client, reviewer, run)

    assert "Claimed but dropped" in body
    assert "cannot add a release the catalog does not already hold" in body
    assert "Unchanged" not in body


def test_the_level_field_says_what_it_applies_to(client, submitter, reviewer):
    """It is a ceiling on this run's evidence, not a value written to a listing, and it was
    labelled as though it were per-listing or per-version."""
    run, _ = _listed(submitter)

    body = _body(client, reviewer, run)

    assert "A ceiling on what this run" in body
    assert "and no other" in body


def test_the_assignment_form_is_the_inline_override(client, submitter, reviewer):
    """One box. The form is still there, collapsed behind the same checkbox-and-CSS mechanism as
    the machine identity and the per-part corrections, so the ordinary case reads as a statement
    rather than as a form to fill in."""
    run, _ = _listed(submitter)

    body = _body(client, reviewer, run)

    assert "Attest a different listing" in body
    assert reverse("review:run_assign_listing", args=[run.pk]) in body
    assert "reveal-fields" in body
    # By the headings that used to exist, not by counting <h2> on the page - several other cards
    # use the same heading class, which is what made the first version of this assert 5 == 1.
    assert "Proposed catalog listing" not in body
    assert "Assign a listing" not in body


def test_the_components_appear_once(client, submitter, reviewer):
    """They were listed twice: a grouped read-only preview and a flat editable form over the same
    entries, so the same part appeared under two headings with different information beside
    each."""
    run, _ = _listed(submitter)

    body = _body(client, reviewer, run)

    assert "Will be attached on approval" not in body
    assert "Adjust before approving" not in body
    assert body.count("Components this run is evidence for") == 1
    # Still grouped by kind, which was the read-only list's one advantage, and still editable.
    assert "MOTHERBOARD" in body.upper()
    assert 'name="included_ties"' in body


def test_each_component_is_named_once(client, submitter, reviewer):
    """The specific duplication, measured: the CPU string appeared in the preview and again in
    the form's model box."""
    run, _ = _listed(submitter)

    body = _body(client, reviewer, run)
    # Scoped to the components section. The run summary at the top of the page names the CPU too,
    # legitimately, and counting the whole page made this read 3 where 2 was correct.
    section = body[body.index("Components this run is evidence for"):]

    assert section.count(run.cpu_model) == 2, (
        "the reported string belongs in the row's heading and in its (collapsed) model box, "
        "not in a second copy of the whole list"
    )


# --- what the validation level actually reaches -----------------------------------
#
# Measured end to end rather than described, because the reported question was exactly this and
# every part of the answer is in a different function. A vendor-tier run on a Dell listing, with
# the CPU and GPU made by somebody else:
#
#     SYSTEM Dell PowerEdge R760      badge=vendor
#        AlmaLinux 9.6+   level=vendor       attestations=1
#     COMPONENT Dell 0M83RH           badge=vendor      (Dell's own board)
#     COMPONENT Intel Xeon ... 4th Gen badge=community  (not Dell's to certify)
#     COMPONENT NVIDIA L40S           badge=community
#
# One attestation per listing, one version row each, and the tier capped per listing.


def test_the_level_applies_to_every_listing_the_run_attests(submitter, reviewer):
    """Not "only the system". Approving attests the system *and* each attached part, each with
    its own attestation."""
    run, listing = _listed(submitter)
    run.claimed_validation_level = "community"
    run.save(update_fields=["claimed_validation_level"])

    services.approve_run(_ready(run), by=reviewer)

    run.refresh_from_db()
    attested = [listing, *run.listing_components.all()]
    assert len(attested) > 1, "the fixture has parts as well as a system"
    for target in attested:
        assert target.versions.filter(
            release=run.alma_release, attestations__isnull=False,
        ).exists(), target


def test_only_the_release_the_run_passed_on_gains_a_tier(submitter, reviewer):
    """Not "all versions". The attestation hangs off one ``ListingVersion``, so a listing already
    carrying other releases keeps whatever their own evidence earned."""
    from lumina.hardware.models import ListingVersion
    from lumina.releases.models import AlmaLinuxRelease

    run, listing = _listed(submitter)
    eight = AlmaLinuxRelease.objects.get_or_create(major=8, defaults={"supported": True})[0]
    older = ListingVersion.objects.create(
        listing_system=listing, release=eight,
        source=ListingVersion.SOURCE_DECLARED,
    )

    services.approve_run(_ready(run), by=reviewer)

    older.refresh_from_db()
    assert older.validation_level == "", "a declared release with no evidence keeps no tier"
    proven = listing.versions.get(release=run.alma_release)
    assert proven.validation_level == "community"


def test_a_vendor_tier_reaches_only_that_vendors_parts(submitter, reviewer):
    """Not "components" as a group. ``effective_level`` caps per listing, so a Dell-attributed run
    cannot make an Intel CPU vendor-validated - the point of the per-listing cap."""
    from lumina.vendors.models import Vendor

    run, listing = _listed(submitter)
    dell = listing.vendor
    dell.verified = True
    dell.save(update_fields=["verified"])
    VendorMembership.objects.create(
        user=submitter, vendor=dell, role=VendorMembership.ROLE_SUBMITTER,
    )
    run.on_behalf_of = dell
    run.claimed_validation_level = "vendor"
    run.save(update_fields=["on_behalf_of", "claimed_validation_level"])

    services.approve_run(_ready(run), by=reviewer)

    run.refresh_from_db()
    assert services.effective_level(run, listing) == "vendor"
    others = [
        component for component in run.listing_components.all()
        if component.vendor_id != dell.pk
    ]
    assert others, "the fixture has parts Dell did not make"
    for component in others:
        assert services.effective_level(run, component) == "community", component
        assert component.vendor.name != "Dell Inc."
    assert Vendor.objects.filter(pk=dell.pk).exists()


# --- the kernel -------------------------------------------------------------------


def test_the_kernel_is_listed(client, submitter, reviewer):
    """Reported: the summary block should list it. "AlmaLinux 9.6" does not say which kernel
    proved the hardware, and a machine needing a driver shipped in a later kernel passes on one
    and fails on another."""
    run = _run(submitter)
    assert run.kernel, "the fixture reports one"

    body = _body(client, reviewer, run)

    assert "Kernel" in body
    assert run.kernel in body


def test_the_kernel_row_is_hidden_when_nothing_reported(submitter):
    run = _run(submitter)
    run.environment = {}
    run.save(update_fields=["environment"])

    assert run.kernel == ""


def test_unknown_reads_as_absent(submitter):
    """The suite writes "unknown" when ``uname`` fails, and a literal "unknown" on a review page
    reads as a finding rather than as a gap."""
    run = _run(submitter)
    run.environment = {"os": {"kernel": "unknown"}}
    run.save(update_fields=["environment"])

    assert run.kernel == ""


@pytest.mark.parametrize("value,expected", [
    (0, []),
    (1, ["a proprietary module was loaded"]),
    (4, ["the CPU is out of spec or unsupported"]),
    (4096, ["an out-of-tree module was loaded"]),
    (5, ["a proprietary module was loaded", "the CPU is out of spec or unsupported"]),
    (1 << 15, ["the kernel was live patched"]),
])
def test_taint_is_decoded_bit_by_bit(submitter, value, expected):
    """Not one blanket explanation. The first version of this said "out-of-tree or proprietary
    code was loaded" for any non-zero value, which is wrong for the value the reported run
    actually carries: 4 is an out-of-spec CPU and has nothing to do with modules.

    The reasons mean different things to somebody weighing evidence - a proprietary module says
    the pass may be about that module, a recent oops says the machine was unwell during the run,
    live patching says the running kernel is not what the version string names.
    """
    run = _run(submitter)
    run.environment = {"kernel_taint": value}
    run.save(update_fields=["environment"])

    assert run.kernel_taint_reasons == expected
    assert run.kernel_tainted is bool(value)


def test_the_taint_badge_carries_the_raw_value(client, submitter, reviewer):
    """So a bit this build does not know about is still visible rather than swallowed."""
    run = _run(submitter)
    run.environment = {"os": {"kernel": "5.14.0-503.el9.x86_64"}, "kernel_taint": 4}
    run.save(update_fields=["environment"])

    body = _body(client, reviewer, run)

    assert "tainted (4)" in body
    assert "The CPU is out of spec or unsupported" in body


def test_a_clean_kernel_says_nothing_about_taint(client, submitter, reviewer):
    run = _run(submitter)
    run.environment = {"os": {"kernel": "5.14.0-503.el9.x86_64"}, "kernel_taint": 0}
    run.save(update_fields=["environment"])

    body = _body(client, reviewer, run)

    assert "tainted" not in body


# --- a driver the suite loaded itself -------------------------------------------
#
# The suite installs the NVIDIA driver and then tries to bring it up in the running kernel rather
# than making somebody reboot, which usually works. When it does, the machine that produced the
# results is running a configuration no boot has applied: the install writes
# rd.driver.blacklist=nouveau to the kernel command line and regenerates the initramfs, and the next
# boot is the first to use either. With attestation coupled to the verdict, a reviewer has to be able
# to see that, and before this nothing in the report said it.


def _with_driver(submitter, **record):
    run = _run(submitter)
    run.environment = {"os": {"kernel": "5.14.0-503.el9.x86_64"}, "nvidia_driver": record}
    run.save(update_fields=["environment"])
    return run


def test_a_hot_loaded_driver_is_visible_to_the_reviewer(client, submitter, reviewer):
    body = _body(client, reviewer, _with_driver(
        submitter, loaded_by_alma_cert=True, modules_after=["nvidia", "nvidia_uvm"],
    ))

    assert "loaded during the run" in body
    assert "rather than at boot" in body


def test_a_driver_that_was_up_at_boot_says_nothing(client, submitter, reviewer):
    """The ordinary case. A note on every GPU run would train reviewers to ignore it."""
    body = _body(client, reviewer, _with_driver(
        submitter, loaded_by_alma_cert=False, present_before_run=True,
    ))

    assert "loaded during the run" not in body


def test_a_machine_with_no_nvidia_card_says_nothing(client, submitter, reviewer):
    run = _run(submitter)
    run.environment = {"os": {"kernel": "5.14.0-503.el9.x86_64"}}
    run.save(update_fields=["environment"])

    assert "loaded during the run" not in _body(client, reviewer, run)


def test_a_run_from_a_suite_older_than_the_field_is_not_a_crash(submitter):
    """Every run ingested before the suite recorded this, which is all of them so far."""
    run = _run(submitter)
    run.environment = {"os": {"kernel": "5.14.0-503.el9.x86_64"}}

    assert run.nvidia_driver == {}
    assert run.driver_loaded_during_run is False
    assert run.driver_load_notes == []


def test_a_garbled_record_is_not_a_crash(submitter):
    """The field arrives from a collector on somebody else's machine, so its shape is an assumption
    rather than a guarantee."""
    run = _run(submitter)
    run.environment = {"nvidia_driver": "yes"}

    assert run.nvidia_driver == {}
    assert run.driver_loaded_during_run is False


@pytest.mark.parametrize("record,expected", [
    ({"installed_during_run": ["dnf -y install nvidia-open cuda"]},
     "The driver was installed during this run as well."),
    ({"install_failed_at": "dnf -y install nvidia-open cuda"},
     "The install did not finish: dnf -y install nvidia-open cuda."),
    ({"newer_kernel_installed": True},
     "A newer kernel is installed, so the next boot is not the kernel these results came from."),
    ({"modules_after": ["nvidia"]},
     "nvidia_uvm was not loaded, and every CUDA call needs it."),
])
def test_what_made_a_hot_load_unusual_is_spelled_out(submitter, record, expected):
    """Only the facts that change how the result should be read, and only when they are true, so an
    ordinary hot load shows one line and an unusual one shows what made it unusual."""
    run = _run(submitter)
    run.environment = {"nvidia_driver": dict(record, loaded_by_alma_cert=True)}

    assert expected in run.driver_load_notes


def test_an_ordinary_hot_load_has_nothing_extra_to_add(submitter):
    run = _run(submitter)
    run.environment = {"nvidia_driver": {
        "loaded_by_alma_cert": True, "modules_after": ["nvidia", "nvidia_uvm"],
        "newer_kernel_installed": False,
    }}

    assert run.driver_load_notes == []


def test_the_notes_reach_the_page(client, submitter, reviewer):
    body = _body(client, reviewer, _with_driver(
        submitter, loaded_by_alma_cert=True, modules_after=["nvidia"],
        installed_during_run=["dnf -y install nvidia-open cuda"],
    ))

    assert "The driver was installed during this run as well." in body
    # Lowercase, because it is a module name. ``capfirst`` was tried here and rendered it
    # ``Nvidia_uvm``, which is not the name of anything.
    assert "nvidia_uvm was not loaded" in body
