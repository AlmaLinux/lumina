"""A vendor claim holds only for the listings that vendor actually makes.

One run touches several listings: a Dell PowerEdge, Dell's board, Intel's CPU family,
NVIDIA's GPU. ``effective_level`` resolved the tier once per *run* from ``on_behalf_of`` and
applied it to all of them, so a Dell-attributed run came out like this:

    Component  NVIDIA L40S                        level=vendor
    Component  Intel Xeon Scalable 4th Generation level=vendor
    Component  Dell Inc. 0M83RH                   level=vendor
    System     Dell Inc. PowerEdge R760           level=vendor

Intel's and NVIDIA's family pages read "Vendor-validated" on the strength of a run those
companies had nothing to do with. A vendor tier says "the company that makes this thing
validated it", so it can only hold where that is true.

The fallback is what the submitter is entitled to on their own: ``almalinux`` for a Foundation
certifier, community otherwise. ``derive_allowed_levels`` never returns the vendor tier
without a named vendor, so that falls out of passing ``None`` rather than being a special
case.

This also unlocked the other half: component vendors can now be *offered* as attribution
targets, because an Intel-attributed run gets vendor on Intel's CPU and community on the Dell
system rather than Intel certifying Dell's chassis. Without the cap, widening the dropdown
would have been the same bug in reverse.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import Group, User
from django.urls import reverse

from lumina.core.certification import ValidationLevel
from lumina.hardware.models import CommunityAttestation, System
from lumina.releases.models import AlmaLinuxRelease
from lumina.results import ingest, services
from lumina.results.models import TestRun
from lumina.results.tests import factories as f
from lumina.results.tests.helpers import release
from lumina.vendors.models import Vendor, VendorMembership

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def alma_nine():
    AlmaLinuxRelease.objects.get_or_create(major=9, defaults={"supported": True})


@pytest.fixture
def dell():
    return Vendor.objects.create(name="Dell Inc.", verified=True, published=True)


@pytest.fixture
def reviewer():
    user = User.objects.create_user("tier-rev")
    group, _ = Group.objects.get_or_create(name="reviewer")
    user.groups.add(group)
    return user


def _member(user, vendor, role=VendorMembership.ROLE_SUBMITTER):
    VendorMembership.objects.create(user=user, vendor=vendor, role=role)


def _approved_run(submitter, reviewer, *, on_behalf_of=None, claimed=""):
    run = ingest.ingest_bundle(
        submitter=submitter, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"],
            results=[f.validate_result("validate.cpu.functional")],
        ))),
    )
    if on_behalf_of is not None or claimed:
        run.on_behalf_of = on_behalf_of
        run.claimed_validation_level = claimed
        run.save(update_fields=["on_behalf_of", "claimed_validation_level"])
    services.approve_run(release(run), by=reviewer)
    return run


def _levels(run):
    """Attested level per listing name."""
    out = {}
    for a in CommunityAttestation.objects.select_related("version"):
        listing = a.version.listing_system or a.version.listing_component
        out[str(listing)] = a.level
    return out


# --- the cap ---------------------------------------------------------------------


def test_a_vendor_claim_stops_at_that_vendors_own_listings(dell, reviewer):
    """The reported bug, measured. Dell's own machine and board get vendor; the Intel CPU
    family and NVIDIA GPU fall back to community."""
    eng = User.objects.create_user("dell-eng")
    _member(eng, dell)
    System.objects.create(vendor=dell, name="PowerEdge R760", owner_vendor=dell)

    run = _approved_run(eng, reviewer, on_behalf_of=dell,
                        claimed=ValidationLevel.VENDOR)

    levels = _levels(run)
    assert levels["Dell Inc. PowerEdge R760"] == ValidationLevel.VENDOR
    assert levels["Dell Inc. 0M83RH"] == ValidationLevel.VENDOR
    assert levels["Intel Intel Xeon Scalable 4th Generation"] == (
        ValidationLevel.COMMUNITY
    )
    assert levels["NVIDIA L40S"] == ValidationLevel.COMMUNITY


def test_a_certifier_keeps_almalinux_on_everything(reviewer):
    """The Foundation is not claiming to be any of these manufacturers, so nothing about
    whose listing it is caps them. Their tier applies throughout."""
    sig = User.objects.create_user("sig-member")
    group, _ = Group.objects.get_or_create(name="certifier")
    sig.groups.add(group)

    run = _approved_run(sig, reviewer, claimed=ValidationLevel.ALMALINUX)

    levels = _levels(run)
    assert set(levels.values()) == {ValidationLevel.ALMALINUX}, levels


def test_a_community_run_is_unchanged(reviewer):
    plain = User.objects.create_user("plain-runner")

    run = _approved_run(plain, reviewer)

    assert set(_levels(run).values()) == {ValidationLevel.COMMUNITY}


# --- self-service re-assessment after joining the vendor -------------------------


def _own_the_system(run, vendor):
    run.refresh_from_db()
    system = run.listing_system
    system.owner_vendor = vendor
    system.save(update_fields=["owner_vendor"])
    return system


def test_a_community_run_can_be_reassessed_to_vendor_tier(dell, reviewer):
    """Validate as a community member, later join the verified vendor that owns the listing, and
    upgrade your own evidence to the vendor tier without re-running the suite."""
    eng = User.objects.create_user("late-joiner")
    run = _approved_run(eng, reviewer)
    assert _levels(run)["Dell Inc. PowerEdge R760"] == ValidationLevel.COMMUNITY

    _own_the_system(run, dell)
    _member(eng, dell)

    assert services.reassessable_as(run, eng) == dell
    services.reassess_run_as_vendor(run, vendor=dell, by=eng)

    # Only the vendor's own listing lifts; the Intel/NVIDIA parts stay capped at community.
    levels = _levels(run)
    assert levels["Dell Inc. PowerEdge R760"] == ValidationLevel.VENDOR
    assert levels["Intel Intel Xeon Scalable 4th Generation"] == ValidationLevel.COMMUNITY


def test_reassess_needs_a_verified_vendor_the_user_belongs_to(reviewer, dell):
    eng = User.objects.create_user("outsider")
    unverified = Vendor.objects.create(name="NewCo", published=True)  # verified defaults False
    run = _approved_run(eng, reviewer)

    _own_the_system(run, dell)
    assert services.reassessable_as(run, eng) is None, "not a member of dell"

    _member(eng, dell)
    _member(eng, unverified)
    _own_the_system(run, unverified)
    assert services.reassessable_as(run, eng) is None, "member, but the owner vendor is unverified"


def test_reassess_is_not_offered_on_someone_elses_run(dell, reviewer):
    owner = User.objects.create_user("run-owner")
    other = User.objects.create_user("bystander")
    run = _approved_run(owner, reviewer)
    _own_the_system(run, dell)
    _member(other, dell)

    assert services.reassessable_as(run, other) is None


def test_reassess_rejects_an_ineligible_post(dell, reviewer):
    """The service re-checks eligibility, so a forged POST cannot upgrade a run the user has no
    standing to."""
    eng = User.objects.create_user("no-standing")
    run = _approved_run(eng, reviewer)

    with pytest.raises(PermissionError):
        services.reassess_run_as_vendor(run, vendor=dell, by=eng)


def test_the_run_page_offers_the_button_and_the_post_upgrades(client, dell, reviewer):
    from django.urls import reverse

    eng = User.objects.create_user("late-joiner-view")
    run = _approved_run(eng, reviewer)
    _own_the_system(run, dell)
    _member(eng, dell)
    client.force_login(eng)

    body = client.get(reverse("results:run_detail", args=[run.uuid])).content.decode()
    assert "Re-assess as Dell Inc." in body

    resp = client.post(reverse("results:reassess_run", args=[run.uuid]))
    assert resp.status_code == 302

    assert _levels(run)["Dell Inc. PowerEdge R760"] == ValidationLevel.VENDOR


def test_an_ineligible_reassess_post_is_forbidden(client, reviewer):
    from django.urls import reverse

    eng = User.objects.create_user("no-standing-view")
    run = _approved_run(eng, reviewer)  # never joined any vendor
    client.force_login(eng)

    resp = client.post(reverse("results:reassess_run", args=[run.uuid]))

    assert resp.status_code == 403


def test_a_component_vendors_run_certifies_their_component_only(reviewer):
    """The case the widening exists for. Intel validating their CPU inside a Dell box:
    vendor on the CPU family, community on the machine that happens to hold it."""
    intel, _ = Vendor.objects.get_or_create(
        name="Intel", defaults={"verified": True, "published": True},
    )
    intel.verified = True
    intel.save(update_fields=["verified"])
    eng = User.objects.create_user("intel-eng")
    _member(eng, intel)

    run = _approved_run(eng, reviewer, on_behalf_of=intel,
                        claimed=ValidationLevel.VENDOR)

    levels = _levels(run)
    assert levels["Intel Intel Xeon Scalable 4th Generation"] == (
        ValidationLevel.VENDOR
    )
    assert levels["Dell Inc. 0M83RH"] == ValidationLevel.COMMUNITY
    assert levels["NVIDIA L40S"] == ValidationLevel.COMMUNITY


def test_the_maintainer_of_a_listing_also_counts(dell, reviewer):
    """A community-catalogued listing has no ``owner_vendor``, so requiring that alone would
    deny a vendor their own hardware. Manufacturer or maintainer, either is the company
    behind the listing."""
    # The listing's *vendor* is Dell so the run auto-links to it, but its maintainer is
    # somebody else - a rebadger, or a partner who took over the entry. Attribution is to the
    # maintainer, whose pk matches neither the reported vendor nor ``listing.vendor``.
    partner = Vendor.objects.create(
        name="Partner Integrations", verified=True, published=True,
    )
    eng = User.objects.create_user("dell-eng-2")
    _member(eng, partner)
    System.objects.create(
        vendor=dell, name="PowerEdge R760", owner_vendor=partner,
    )

    run = _approved_run(eng, reviewer, on_behalf_of=partner,
                        claimed=ValidationLevel.VENDOR)

    levels = _levels(run)
    assert levels["Dell Inc. PowerEdge R760"] == ValidationLevel.VENDOR, levels
    # And it does not leak onto Dell's board, which the partner does not maintain.
    assert levels["Dell Inc. 0M83RH"] == ValidationLevel.COMMUNITY


def test_the_listing_can_only_withhold_never_grant(reviewer, dell):
    """The rule this replaces went the other way and was wrong for it: falling back to
    ``listing.owner_vendor`` treated a Foundation certifier validating a Dell machine as
    submitting *for* Dell, so their run came out vendor-validated.

    A plain community member's run against a Dell-owned listing must still be community.
    """
    System.objects.create(vendor=dell, name="PowerEdge R760", owner_vendor=dell)
    plain = User.objects.create_user("passer-by")

    run = _approved_run(plain, reviewer)

    assert _levels(run)["Dell Inc. PowerEdge R760"] == ValidationLevel.COMMUNITY


# --- the family question ---------------------------------------------------------


def test_a_cpu_model_attests_its_whole_family(reviewer):
    """What "does a vendor attestation for one model apply to the family" resolves to:
    it already does, because the *tie* is the family.

    ``silicon_component`` prefers a curated family over creating a model-level entry, so the
    attestation lands on "Intel Xeon Scalable 4th Generation" and there is no per-model row to
    attest instead. Certification is family-level throughout the catalog by design.
    """
    intel, _ = Vendor.objects.get_or_create(
        name="Intel", defaults={"verified": True, "published": True},
    )
    intel.verified = True
    intel.save(update_fields=["verified"])
    eng = User.objects.create_user("intel-eng-2")
    _member(eng, intel)

    run = _approved_run(eng, reviewer, on_behalf_of=intel,
                        claimed=ValidationLevel.VENDOR)

    tied = {c.name: c for c in run.listing_components.all() if c.kind == "cpu"}
    assert list(tied) == ["Intel Xeon Scalable 4th Generation"]
    # And nothing model-level was created beside it.
    from lumina.hardware.models import Component
    assert not Component.objects.filter(name__icontains="6430").exists()


def test_the_model_behind_a_family_attestation_is_recoverable(reviewer):
    """Which keeps "within reason" answerable rather than lost.

    The family carries the tier; the specific processor that produced the evidence stays on
    the run, so a page can say *which* members of a family have actually been validated
    instead of implying all of them were.
    """
    intel, _ = Vendor.objects.get_or_create(
        name="Intel", defaults={"verified": True, "published": True},
    )
    eng = User.objects.create_user("intel-eng-3")
    _member(eng, intel)

    _approved_run(eng, reviewer, on_behalf_of=intel)

    attestation = CommunityAttestation.objects.filter(
        version__listing_component__kind="cpu",
    ).select_related("test_run").first()
    assert attestation is not None
    assert attestation.test_run.cpu_model == "Intel(R) Xeon(R) Gold 6430"


# --- the recompute pass ----------------------------------------------------------


def test_the_command_fixes_rows_written_before_the_cap(dell, reviewer):
    """An attestation's level is frozen at approval and the listing's badge derives from it,
    so rows written before the cap keep saying Dell vendor-certified Intel's CPU. Nothing
    recomputes them on its own."""
    from io import StringIO

    from django.core.management import call_command

    eng = User.objects.create_user("dell-eng-3")
    _member(eng, dell)
    System.objects.create(vendor=dell, name="PowerEdge R760", owner_vendor=dell)
    run = _approved_run(eng, reviewer, on_behalf_of=dell,
                        claimed=ValidationLevel.VENDOR)
    # Put the pre-cap state back by hand: one tier for everything the run touched.
    CommunityAttestation.objects.filter(test_run=run).update(
        level=ValidationLevel.VENDOR,
    )

    call_command("recompute_attestation_levels", stdout=StringIO())

    levels = _levels(run)
    assert levels["Intel Intel Xeon Scalable 4th Generation"] == (
        ValidationLevel.COMMUNITY
    )
    assert levels["NVIDIA L40S"] == ValidationLevel.COMMUNITY
    assert levels["Dell Inc. PowerEdge R760"] == ValidationLevel.VENDOR


def test_the_command_also_redoes_the_derived_badge(dell, reviewer):
    """The listing's own ``validation_level`` is a rollup of its attestations, so fixing the
    rows without recomputing would leave the badge reading vendor."""
    from io import StringIO

    from django.core.management import call_command

    from lumina.hardware.models import Component

    eng = User.objects.create_user("dell-eng-4")
    _member(eng, dell)
    run = _approved_run(eng, reviewer, on_behalf_of=dell,
                        claimed=ValidationLevel.VENDOR)
    CommunityAttestation.objects.filter(test_run=run).update(
        level=ValidationLevel.VENDOR,
    )
    cpu = Component.objects.get(name="Intel Xeon Scalable 4th Generation")
    cpu.validation_level = ValidationLevel.VENDOR
    cpu.save(update_fields=["validation_level"])

    call_command("recompute_attestation_levels", stdout=StringIO())

    cpu.refresh_from_db()
    assert cpu.validation_level == ValidationLevel.COMMUNITY


def test_the_command_is_idempotent(dell, reviewer):
    from io import StringIO

    from django.core.management import call_command

    eng = User.objects.create_user("dell-eng-5")
    _member(eng, dell)
    run = _approved_run(eng, reviewer, on_behalf_of=dell,
                        claimed=ValidationLevel.VENDOR)
    before = _levels(run)

    out = StringIO()
    call_command("recompute_attestation_levels", stdout=out)

    assert _levels(run) == before
    assert "Re-derived 0 attestation(s)" in out.getvalue()


def test_dry_run_writes_nothing(dell, reviewer):
    from io import StringIO

    from django.core.management import call_command

    eng = User.objects.create_user("dell-eng-6")
    _member(eng, dell)
    run = _approved_run(eng, reviewer, on_behalf_of=dell,
                        claimed=ValidationLevel.VENDOR)
    CommunityAttestation.objects.filter(test_run=run).update(
        level=ValidationLevel.VENDOR,
    )

    out = StringIO()
    call_command("recompute_attestation_levels", "--dry-run", stdout=out)

    assert "would change" in out.getvalue()
    assert _levels(run)["NVIDIA L40S"] == ValidationLevel.VENDOR, "it wrote anyway"


def test_submission_sourced_rows_are_left_alone(dell, reviewer):
    """They were never subject to this. ``Submission.approve`` caps them at
    ``MANUAL_CEILING`` and has no per-listing vendor claim to get wrong."""
    from io import StringIO

    from django.core.management import call_command

    from lumina.hardware.models import ListingVersion, Submission

    listing = System.objects.create(vendor=dell, name="PowerEdge R999")
    version = ListingVersion.objects.create(
        listing_system=listing,
        release=AlmaLinuxRelease.objects.get(major=9),
        source=ListingVersion.SOURCE_DECLARED,
    )
    person = User.objects.create_user("manual-person")
    submission = Submission.objects.create(
        submitter=person, listing_system=listing,
        claimed_validation_level=ValidationLevel.COMMUNITY,
    )
    CommunityAttestation.objects.create(
        version=version, listing_system=listing, attested_by=person,
        level=ValidationLevel.VENDOR, submission=submission,
    )

    call_command("recompute_attestation_levels", stdout=StringIO())

    assert CommunityAttestation.objects.get(
        submission=submission
    ).level == ValidationLevel.VENDOR


# --- setting a component's level --------------------------------------------------
#
# Reported: "Where can the validation level be set on components? I am on the reviewer account
# which is a member of the Intel vendor, yet I don't see how I can upgrade the CPU to vendor
# validated."
#
# Nowhere, was the answer. A component's tier derives entirely from its attestations, the tier
# on a run applied only to the machine, and the ``validation_level`` field in the Django admin
# is a trap - writable, then overwritten by the next ``recompute_listing_levels``.
#
# And the first attempt at fixing it put the control in the wrong place. Adding component
# vendors to the run's "Validating as" dropdown was reported straight back: "I still see
# 'validating as' with Intel in the dropdown even though this is a Dell system... That's
# misleading at best." Correct - the data model capped the tier per listing, but a field
# labelled "Validating as" on a form about a Dell system offering "Intel" reads as Intel
# validating the Dell system. The claim belongs on the part it is about.


def _claim_key(run, kind="cpu"):
    return next(
        e["key"] for e in services.preview_component_ties(run) if e["kind"] == kind
    )


@pytest.fixture
def intel():
    vendor, _ = Vendor.objects.get_or_create(
        name="Intel", defaults={"published": True},
    )
    vendor.verified = True
    vendor.save(update_fields=["verified"])
    return vendor


def test_the_claim_is_offered_on_the_part_not_the_run(intel):
    """The control appears against the Intel CPU and nowhere else - not on Dell's board, not
    on the NVIDIA GPU, and not as a choice about the machine."""
    from lumina.results.forms import RunListingProposalForm

    eng = User.objects.create_user("intel-a")
    _member(eng, intel)
    run = ingest.ingest_bundle(
        submitter=eng, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"],
            results=[f.validate_result("validate.cpu.functional")],
        ))),
    )

    form = RunListingProposalForm(run=run, user=eng)
    offered = {
        row["kind"]: bool(row.get("claim_field")) for row in form.component_rows
    }

    assert offered["cpu"] is True
    assert offered["motherboard"] is False
    assert offered["gpu"] is False


def test_the_claim_makes_only_that_component_vendor_validated(intel, reviewer):
    """The whole point. Intel validating their silicon inside a Dell chassis says nothing
    about the chassis."""
    eng = User.objects.create_user("intel-b")
    _member(eng, intel)
    run = ingest.ingest_bundle(
        submitter=eng, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"],
            results=[f.validate_result("validate.cpu.functional")],
        ))),
    )
    run.component_overrides = {_claim_key(run): {"attribute_to": intel.slug}}
    run.save(update_fields=["component_overrides"])

    services.approve_run(release(run), by=reviewer)

    levels = _levels(run)
    assert levels["Intel Intel Xeon Scalable 4th Generation"] == (
        ValidationLevel.VENDOR
    )
    assert levels["Dell Inc. 0M83RH"] == ValidationLevel.COMMUNITY
    assert levels["NVIDIA L40S"] == ValidationLevel.COMMUNITY


def test_an_unanswered_claim_still_holds(intel, reviewer):
    """The box is shown ticked, so a run nobody edited carries the claim.

    This test used to assert the opposite, under the heading "Opt-in. Being an Intel member does
    not silently make every run they submit an Intel certification of the silicon in it." That
    rationale was already stale: it was written while the box rendered *unticked*, and the box has
    been ticked by default since - a deliberate decision, on the grounds that certifying a part
    the catalog already holds is the normal case for a component vendor.

    What was left was a display that said one thing and an engine that did another. A submitter
    who never opens the listing-details form, or opens it and submits from the run page without
    saving, saw three ticked "Certify as Intel" boxes and got community. Reported twice, the
    second time as "the box was ticked and it didn't certify the components as intel".

    So this is not a policy change, it is the engine catching up with the control. Declining is
    the act that gets recorded, and an explicit decline still wins.

    Nothing is granted that the submitter could not claim: ``effective_level`` still caps by
    their standing, and the claim still reaches only listings that vendor makes.
    """
    eng = User.objects.create_user("intel-c")
    _member(eng, intel)
    run = ingest.ingest_bundle(
        submitter=eng, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"],
            results=[f.validate_result("validate.cpu.functional")],
        ))),
    )

    services.approve_run(release(run), by=reviewer)

    assert _levels(run)["Intel Intel Xeon Scalable 4th Generation"] == (
        ValidationLevel.VENDOR
    )


def test_declining_it_keeps_the_component_community(intel, reviewer):
    """The half that has to keep working: unticking the box is a decision, stored as an empty
    ``attribute_to``, and it beats the default."""
    eng = User.objects.create_user("intel-decline")
    _member(eng, intel)
    run = ingest.ingest_bundle(
        submitter=eng, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"],
            results=[f.validate_result("validate.cpu.functional")],
        ))),
    )
    run.component_overrides = {_claim_key(run): {"attribute_to": ""}}
    run.save(update_fields=["component_overrides"])

    services.approve_run(release(run), by=reviewer)

    assert _levels(run)["Intel Intel Xeon Scalable 4th Generation"] == (
        ValidationLevel.COMMUNITY
    )


def test_a_non_member_gets_nothing_by_default(intel, reviewer):
    """The default is not a free upgrade. It applies only where the box would have been offered,
    which needs a verified vendor and a submitter who represents it."""
    outsider = User.objects.create_user("intel-outsider")
    run = ingest.ingest_bundle(
        submitter=outsider, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"],
            results=[f.validate_result("validate.cpu.functional")],
        ))),
    )

    services.approve_run(release(run), by=reviewer)

    assert _levels(run)["Intel Intel Xeon Scalable 4th Generation"] == (
        ValidationLevel.COMMUNITY
    )


def test_an_unverified_vendor_gets_nothing_by_default(intel, reviewer):
    eng = User.objects.create_user("intel-unverified")
    _member(eng, intel)
    intel.verified = False
    intel.save(update_fields=["verified"])
    run = ingest.ingest_bundle(
        submitter=eng, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"],
            results=[f.validate_result("validate.cpu.functional")],
        ))),
    )

    services.approve_run(release(run), by=reviewer)

    assert _levels(run)["Intel Intel Xeon Scalable 4th Generation"] == (
        ValidationLevel.COMMUNITY
    )


def test_a_claim_naming_a_vendor_the_submitter_does_not_represent_grants_nothing(
    intel, reviewer,
):
    """The stored claim is an input, not an authority. ``resolve_claimed_level`` still caps by
    the submitter's standing, so a hand-written override cannot mint a vendor tier."""
    outsider = User.objects.create_user("outsider")
    run = ingest.ingest_bundle(
        submitter=outsider, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"],
            results=[f.validate_result("validate.cpu.functional")],
        ))),
    )
    run.component_overrides = {_claim_key(run): {"attribute_to": intel.slug}}
    run.save(update_fields=["component_overrides"])

    services.approve_run(release(run), by=reviewer)

    assert _levels(run)["Intel Intel Xeon Scalable 4th Generation"] == (
        ValidationLevel.COMMUNITY
    )


def test_a_claim_cannot_name_a_vendor_that_did_not_make_the_part(intel, reviewer):
    """Even with membership, the claim only holds where the vendor is the part's own. Intel
    cannot certify NVIDIA's GPU by pointing a claim at it."""
    eng = User.objects.create_user("intel-d")
    _member(eng, intel)
    run = ingest.ingest_bundle(
        submitter=eng, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"],
            results=[f.validate_result("validate.cpu.functional")],
        ))),
    )
    run.component_overrides = {
        _claim_key(run, "gpu"): {"attribute_to": intel.slug},
    }
    run.save(update_fields=["component_overrides"])

    services.approve_run(release(run), by=reviewer)

    assert _levels(run)["NVIDIA L40S"] == ValidationLevel.COMMUNITY


def test_the_claim_survives_a_round_trip_through_the_form(client, intel):
    """Stored alongside the part's other corrections, so one place holds everything the
    submitter said about a component and no new column was needed."""
    from django.urls import reverse

    from lumina.results.forms import RunListingProposalForm

    eng = User.objects.create_user("intel-e", password="pw")
    _member(eng, intel)
    run = ingest.ingest_bundle(
        submitter=eng, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"],
            results=[f.validate_result("validate.cpu.functional")],
        ))),
    )
    form = RunListingProposalForm(run=run, user=eng)
    index = next(
        i for i, row in enumerate(form.component_rows) if row["kind"] == "cpu"
    )
    payload = {
        "vendor_name": "Dell Inc.", "name": "PowerEdge R760",
        "machine_kind": "prebuilt",
        "included_ties": [row["key"] for row in form.component_rows],
        "components_submitted": "1",
        f"{form.COMPONENT_CLAIM_PREFIX}{index}": "on",
    }
    for i, row in enumerate(form.component_rows):
        payload[f"{form.COMPONENT_BRAND_PREFIX}{i}"] = row["brand"]
        payload[f"{form.COMPONENT_MODEL_PREFIX}{i}"] = row["raw_model"]
    client.force_login(eng)

    client.post(reverse("results:propose_listing", args=[run.uuid]), payload)

    run.refresh_from_db()
    assert run.component_overrides[_claim_key(run)]["attribute_to"] == intel.slug
    # And it comes back ticked.
    assert RunListingProposalForm(run=run, user=eng).initial[
        f"{RunListingProposalForm.COMPONENT_CLAIM_PREFIX}{index}"
    ] is True
    # It lives in ``component_overrides`` and nowhere else. Found in a devstack run's stored
    # blob as ``tie_claim_1: false, tie_claim_2: false``: the view stripped the brand and model
    # prefixes but not this one, so the checkbox accumulated in the listing proposal and in the
    # audit entry. Inert, since nothing reads it from there - stored nonsense all the same, and
    # the third key to leak into that blob.
    assert not [
        key for key in run.listing_proposal
        if key.startswith(RunListingProposalForm.COMPONENT_CLAIM_PREFIX)
    ], run.listing_proposal


def test_the_page_offers_it(client, intel):
    from django.urls import reverse

    eng = User.objects.create_user("intel-f", password="pw")
    _member(eng, intel)
    run = ingest.ingest_bundle(
        submitter=eng, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"],
            results=[f.validate_result("validate.cpu.functional")],
        ))),
    )
    client.force_login(eng)

    body = " ".join(client.get(
        reverse("results:propose_listing", args=[run.uuid])
    ).content.decode().split())

    assert "Certify as Intel" in body
    assert "Only this part; the rest of the machine is unaffected." in body


def test_a_claim_on_one_part_does_not_carry_to_a_sibling_from_the_same_vendor(
    intel, reviewer,
):
    """Two Intel parts in one machine, one claimed.

    The outer "is this the vendor's own listing" check cannot separate these - both belong to
    Intel - so without matching the claim to the *tie* it was filed against, claiming the CPU
    silently vendor-certified the integrated graphics too. Which is exactly the shape of the
    original bug, one level down.
    """
    from lumina.hardware.models import Component, ComponentRole

    eng = User.objects.create_user("intel-g")
    _member(eng, intel)
    # A curated Intel GPU family, so the integrated graphics resolves to a component at all.
    # Without one the preview reports None and there is no sibling listing to leak onto.
    Component.objects.create(
        vendor=intel, name="Intel UHD Graphics", kind="gpu",
        role=ComponentRole.FAMILY, model_patterns=[r"UHD Graphics \d+"],
        published=True,
    )
    inventory = f.default_inventory()
    inventory["summary"]["gpus"] = [{
        "vendor": "intel", "model": "CometLake-S GT2 [UHD Graphics 630]",
        "driver": "i915", "driver_version": "1.0", "pci": "00:02.0",
    }]
    run = ingest.ingest_bundle(
        submitter=eng, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"], inventory=inventory,
            results=[f.validate_result("validate.cpu.functional")],
        ))),
    )
    ties = {e["kind"]: e for e in services.preview_component_ties(run)}
    assert ties["cpu"]["component"].vendor == ties["gpu"]["component"].vendor, (
        "both parts must be Intel's for this to prove anything"
    )
    # The GPU is *declined* explicitly rather than left unanswered, now that an unanswered box
    # means the claim holds. The point of the test is unchanged: the claim has to be matched to
    # the tie it was filed against, not to "does this listing belong to Intel" - which cannot tell
    # these two apart.
    run.component_overrides = {
        ties["cpu"]["key"]: {"attribute_to": intel.slug},
        ties["gpu"]["key"]: {"attribute_to": ""},
    }
    run.save(update_fields=["component_overrides"])

    services.approve_run(release(run), by=reviewer)

    levels = _levels(run)
    assert levels[str(ties["cpu"]["component"])] == ValidationLevel.VENDOR
    assert levels[str(ties["gpu"]["component"])] == ValidationLevel.COMMUNITY


def test_the_claim_is_not_offered_without_membership(intel):
    """Being able to see the part is not being able to speak for its maker. Without this the
    control appeared for everybody and did nothing - ``effective_level`` re-derives the tier
    from the submitter's standing, so an unentitled tick produces a community attestation."""
    from lumina.results.forms import RunListingProposalForm

    outsider = User.objects.create_user("no-membership")
    run = ingest.ingest_bundle(
        submitter=outsider, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"],
            results=[f.validate_result("validate.cpu.functional")],
        ))),
    )

    form = RunListingProposalForm(run=run, user=outsider)
    cpu = next(row for row in form.component_rows if row["kind"] == "cpu")

    assert cpu.get("claim_field") is None
    assert not any(
        name.startswith(RunListingProposalForm.COMPONENT_CLAIM_PREFIX)
        for name in form.fields
    )


def test_an_unverified_vendor_cannot_be_claimed_for(reviewer):
    """Verification is what makes a vendor claim mean anything, so an unverified vendor is not
    offered even to its own members."""
    from lumina.results.forms import RunListingProposalForm

    intel, _ = Vendor.objects.get_or_create(name="Intel", defaults={"published": True})
    intel.verified = False
    intel.save(update_fields=["verified"])
    eng = User.objects.create_user("intel-h")
    _member(eng, intel)
    run = ingest.ingest_bundle(
        submitter=eng, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"],
            results=[f.validate_result("validate.cpu.functional")],
        ))),
    )

    form = RunListingProposalForm(run=run, user=eng)
    cpu = next(row for row in form.component_rows if row["kind"] == "cpu")

    assert cpu.get("claim_field") is None


# --- who becomes a listing's maintainer -------------------------------------------


def test_only_the_machines_own_vendor_becomes_its_maintainer(intel, reviewer):
    """A run attributed to Intel must not make Intel the maintainer of a Dell machine.

    Found from the report "I still see Intel in the dropdown even though this is a Dell
    system". The cause was not the dropdown: ``create_listings_from_run`` set
    ``owner_vendor=run.on_behalf_of`` unconditionally, so an Intel-attributed run created a
    Dell OptiPlex listing owned by Intel - and since ``identity_vendors`` counts a maintainer
    as a company behind the listing, Intel then looked like a legitimate attribution target
    for that machine forever. Self-reinforcing, and invisible unless somebody asked.
    """
    from lumina.hardware.models import System

    eng = User.objects.create_user("intel-owner")
    _member(eng, intel)
    run = ingest.ingest_bundle(
        submitter=eng, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"],
            results=[f.validate_result("validate.cpu.functional")],
        ))),
    )
    run.on_behalf_of = intel
    run.claimed_validation_level = ValidationLevel.VENDOR
    run.save(update_fields=["on_behalf_of", "claimed_validation_level"])

    services.approve_run(release(run), by=reviewer)

    listing = System.objects.get(name="PowerEdge R760")
    assert listing.vendor.name == "Dell Inc."
    assert listing.owner_vendor is None, (
        "Intel became the maintainer of a Dell machine"
    )


def test_a_vendor_submitting_their_own_hardware_still_becomes_maintainer(reviewer):
    """The case the assignment exists for, which must keep working: it is what gives a vendor
    edit rights over their own listing later."""
    from lumina.hardware.models import System

    dell = Vendor.objects.create(name="Dell Inc.", verified=True, published=True)
    eng = User.objects.create_user("dell-owner")
    _member(eng, dell)
    run = ingest.ingest_bundle(
        submitter=eng, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"],
            results=[f.validate_result("validate.cpu.functional")],
        ))),
    )
    run.on_behalf_of = dell
    run.claimed_validation_level = ValidationLevel.VENDOR
    run.save(update_fields=["on_behalf_of", "claimed_validation_level"])

    services.approve_run(release(run), by=reviewer)

    assert System.objects.get(name="PowerEdge R760").owner_vendor == dell


def test_a_community_run_leaves_the_listing_unowned(reviewer):
    from lumina.hardware.models import System

    plain = User.objects.create_user("unowned-runner")
    run = ingest.ingest_bundle(
        submitter=plain, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"],
            results=[f.validate_result("validate.cpu.functional")],
        ))),
    )

    services.approve_run(release(run), by=reviewer)

    assert System.objects.get(name="PowerEdge R760").owner_vendor is None


def test_the_claim_starts_ticked(intel):
    """If you speak for the company that made this part and the run exercised it, their
    certification is the natural claim - the same reasoning that preselects a machine's own
    vendor in "Validating as"."""
    from lumina.results.forms import RunListingProposalForm

    eng = User.objects.create_user("intel-tick")
    _member(eng, intel)
    run = ingest.ingest_bundle(
        submitter=eng, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"],
            results=[f.validate_result("validate.cpu.functional")],
        ))),
    )

    form = RunListingProposalForm(run=run, user=eng)
    cpu = next(row for row in form.component_rows if row["kind"] == "cpu")

    assert form.initial[cpu["claim_field"].name] is True


def test_unticking_it_survives_a_reload(client, intel):
    """"Never asked" and "asked and declined" cannot both be absence, or unticking would come
    back ticked on the next load and quietly undo itself. That is the trap the release
    checkboxes had, and the decline is recorded for the same reason."""
    from django.urls import reverse

    from lumina.results.forms import RunListingProposalForm

    eng = User.objects.create_user("intel-untick", password="pw")
    _member(eng, intel)
    run = ingest.ingest_bundle(
        submitter=eng, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"],
            results=[f.validate_result("validate.cpu.functional")],
        ))),
    )
    form = RunListingProposalForm(run=run, user=eng)
    payload = {
        "vendor_name": "Dell Inc.", "name": "PowerEdge R760",
        "machine_kind": "prebuilt",
        "included_ties": [row["key"] for row in form.component_rows],
        "components_submitted": "1",
    }
    for i, row in enumerate(form.component_rows):
        payload[f"{form.COMPONENT_BRAND_PREFIX}{i}"] = row["brand"]
        payload[f"{form.COMPONENT_MODEL_PREFIX}{i}"] = row["raw_model"]
    # Every claim box deliberately left out of the payload, i.e. unticked.
    client.force_login(eng)

    client.post(reverse("results:propose_listing", args=[run.uuid]), payload)

    run.refresh_from_db()
    reloaded = RunListingProposalForm(run=run, user=eng)
    cpu = next(row for row in reloaded.component_rows if row["kind"] == "cpu")
    assert reloaded.initial[cpu["claim_field"].name] is False, (
        "the decline was forgotten and the box came back ticked"
    )


def test_a_declined_claim_grants_nothing(client, intel, reviewer):
    """And the decline has to be honoured at approval, not merely remembered by the form."""
    from django.urls import reverse

    from lumina.results.forms import RunListingProposalForm

    eng = User.objects.create_user("intel-declined", password="pw")
    _member(eng, intel)
    run = ingest.ingest_bundle(
        submitter=eng, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"],
            results=[f.validate_result("validate.cpu.functional")],
        ))),
    )
    form = RunListingProposalForm(run=run, user=eng)
    payload = {
        "vendor_name": "Dell Inc.", "name": "PowerEdge R760",
        "machine_kind": "prebuilt",
        "included_ties": [row["key"] for row in form.component_rows],
        "components_submitted": "1",
    }
    for i, row in enumerate(form.component_rows):
        payload[f"{form.COMPONENT_BRAND_PREFIX}{i}"] = row["brand"]
        payload[f"{form.COMPONENT_MODEL_PREFIX}{i}"] = row["raw_model"]
    client.force_login(eng)
    client.post(reverse("results:propose_listing", args=[run.uuid]), payload)

    services.approve_run(release(TestRun.objects.get(pk=run.pk)), by=reviewer)

    assert _levels(run)["Intel Intel Xeon Scalable 4th Generation"] == (
        ValidationLevel.COMMUNITY
    )


def test_saving_with_the_default_records_the_claim(client, intel, reviewer):
    """The other half of the default: opening the form and saving it takes the claim."""
    from django.urls import reverse

    from lumina.results.forms import RunListingProposalForm

    eng = User.objects.create_user("intel-default", password="pw")
    _member(eng, intel)
    run = ingest.ingest_bundle(
        submitter=eng, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"],
            results=[f.validate_result("validate.cpu.functional")],
        ))),
    )
    form = RunListingProposalForm(run=run, user=eng)
    index = next(
        i for i, row in enumerate(form.component_rows) if row["kind"] == "cpu"
    )
    payload = {
        "vendor_name": "Dell Inc.", "name": "PowerEdge R760",
        "machine_kind": "prebuilt",
        "included_ties": [row["key"] for row in form.component_rows],
        "components_submitted": "1",
        # What a browser posts for a box rendered already ticked.
        f"{form.COMPONENT_CLAIM_PREFIX}{index}": "on",
    }
    for i, row in enumerate(form.component_rows):
        payload[f"{form.COMPONENT_BRAND_PREFIX}{i}"] = row["brand"]
        payload[f"{form.COMPONENT_MODEL_PREFIX}{i}"] = row["raw_model"]
    client.force_login(eng)

    client.post(reverse("results:propose_listing", args=[run.uuid]), payload)
    services.approve_run(release(TestRun.objects.get(pk=run.pk)), by=reviewer)

    assert _levels(run)["Intel Intel Xeon Scalable 4th Generation"] == (
        ValidationLevel.VENDOR
    )


# --- a claim is not revoked by a save that never asked about it --------------------
#
# Reported: "I left the 'Certify as intel' box checked and approved the run, but it did not upgrade
# the CPU family to vendor validated for the major version."
#
# The claim was recorded. A later save erased it. ``component_overrides`` rebuilds the whole dict
# from the fields the current form happens to have, so any save whose claim field was not built
# dropped a claim nobody meant to touch. The audit trail for devstack run cf9c7c77 shows the
# sequence exactly:
#
#     19:59:37  test_run.propose_listing      {'on_behalf_of': 'intel', 'claimed_validation_level': 'vendor'}
#     19:59:55  test_run.component_ties_edit  <- the claim disappears here
#     19:59:58  test_run.approve              -> attestations written at community


def _fresh_run(submitter):
    return TestRun.objects.get(pk=ingest.ingest_bundle(
        submitter=submitter, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"],
            results=[f.validate_result("validate.cpu.functional")],
        ))),
    ).pk)


def _claimed_run(client, submitter, intel):
    """A run whose submitter has ticked "Certify as Intel" on the CPU."""
    from lumina.results.forms import RunListingProposalForm

    run = _fresh_run(submitter)
    form = RunListingProposalForm(run=run, user=submitter)
    index = next(i for i, r in enumerate(form.component_rows) if r["kind"] == "cpu")
    payload = {
        "vendor_name": "Dell Inc.", "name": "PowerEdge R760", "machine_kind": "prebuilt",
        "components_submitted": "1",
        "included_ties": [r["key"] for r in form.component_rows],
        f"{form.COMPONENT_CLAIM_PREFIX}{index}": "on",
    }
    for i, row in enumerate(form.component_rows):
        payload[f"{form.COMPONENT_BRAND_PREFIX}{i}"] = row["brand"]
        payload[f"{form.COMPONENT_MODEL_PREFIX}{i}"] = row["raw_model"]
    client.force_login(submitter)
    client.post(reverse("results:propose_listing", args=[run.uuid]), payload)
    run.refresh_from_db()
    assert any(
        chosen.get("attribute_to") for chosen in run.component_overrides.values()
    ), "the fixture must start with a claim recorded"
    return run


def _reviewer_saves_components(client, reviewer, run, **overrides):
    """A reviewer correcting a component, the way the review page posts it."""
    from lumina.results.forms import RunComponentTiesForm

    form = RunComponentTiesForm(run=TestRun.objects.get(pk=run.pk))
    payload = {
        "components_submitted": "1",
        "included_ties": [r["key"] for r in form.component_rows],
    }
    for i, row in enumerate(form.component_rows):
        payload[f"{form.COMPONENT_BRAND_PREFIX}{i}"] = overrides.get(
            row["kind"], row["brand"],
        )
        payload[f"{form.COMPONENT_MODEL_PREFIX}{i}"] = row["raw_model"]
        # A browser posts every box it rendered, and the claim renders ticked.
        if row.get("claim_field"):
            payload[row["claim_field"].name] = "on"
    client.force_login(reviewer)
    client.post(reverse("review:run_component_ties", args=[run.pk]), payload)
    run.refresh_from_db()
    return run


def test_a_reviewer_component_edit_keeps_the_claim(client, intel, reviewer):
    """The reported sequence, end to end."""
    submitter = User.objects.create_user("claim-keep", password="pw")
    _member(submitter, intel)
    run = _claimed_run(client, submitter, intel)

    run = _reviewer_saves_components(client, reviewer, run, motherboard="Dell")

    key = _claim_key(run)
    assert run.component_overrides[key]["attribute_to"] == intel.slug
    board = _claim_key(run, kind="motherboard")
    assert run.component_overrides[board]["brand"] == "Dell", (
        "and the reviewer's actual correction still lands"
    )


def test_a_save_whose_claim_field_was_not_built_keeps_it(client, intel, reviewer):
    """The devstack case. The claim box is only built for a part whose vendor is verified and
    whose maker the submitter represents; when any of that is untrue at the moment of a save, the
    field does not exist and the stored claim used to vanish with it."""
    submitter = User.objects.create_user("claim-nofield", password="pw")
    _member(submitter, intel)
    run = _claimed_run(client, submitter, intel)
    intel.verified = False
    intel.save(update_fields=["verified"])

    run = _reviewer_saves_components(client, reviewer, run, motherboard="Dell")

    assert run.component_overrides[_claim_key(run)]["attribute_to"] == intel.slug


def test_unticking_it_is_still_a_decline(client, intel, reviewer):
    """The distinction that has to survive the fix: a box that *was* rendered and left unticked is
    a decision, and it must not be resurrected by the carry-forward."""
    from lumina.results.forms import RunComponentTiesForm

    submitter = User.objects.create_user("claim-decline", password="pw")
    _member(submitter, intel)
    run = _claimed_run(client, submitter, intel)

    form = RunComponentTiesForm(run=TestRun.objects.get(pk=run.pk))
    payload = {
        "components_submitted": "1",
        "included_ties": [r["key"] for r in form.component_rows],
    }
    for i, row in enumerate(form.component_rows):
        payload[f"{form.COMPONENT_BRAND_PREFIX}{i}"] = row["brand"]
        payload[f"{form.COMPONENT_MODEL_PREFIX}{i}"] = row["raw_model"]
        # every box posted except the claim, which is what unticking looks like
    client.force_login(reviewer)
    client.post(reverse("review:run_component_ties", args=[run.pk]), payload)

    run.refresh_from_db()
    assert run.component_overrides[_claim_key(run)]["attribute_to"] == ""


def test_the_claim_reaches_the_family_after_a_reviewer_edit(client, intel, reviewer):
    """The user's outcome, not just the stored dict: approving after a component edit certifies
    the CPU family at the vendor tier."""
    submitter = User.objects.create_user("claim-e2e", password="pw")
    _member(submitter, intel)
    run = _claimed_run(client, submitter, intel)
    run = _reviewer_saves_components(client, reviewer, run, motherboard="Dell")

    services.approve_run(release(TestRun.objects.get(pk=run.pk)), by=reviewer)

    run.refresh_from_db()
    family = run.listing_components.get(kind="cpu")
    assert family.validation_level == ValidationLevel.VENDOR
    version = family.versions.get(release=run.alma_release)
    assert version.validation_level == ValidationLevel.VENDOR


# --- a decline has to survive everything the submitter does next -------------------
#
# Found by a parallel investigation of the report above, and they matter *because* of the fix
# to it: while an unanswered box meant "no claim", losing a stored decline was invisible. Now
# that the ticked box is honoured, every path that drops an answer refills it with a vendor
# claim the reader had turned down.


def _declined(run, intel, kind="cpu"):
    run.component_overrides = {_claim_key(run, kind): {"attribute_to": ""}}
    run.save(update_fields=["component_overrides"])
    return run


def test_correcting_the_cpu_model_keeps_the_decline(intel, reviewer):
    """The CPU tie used to be keyed on the submitter's *corrected* model, unlike every other
    kind, so tidying "Intel(R) Xeon(R) Gold 6430" to "Intel Xeon Gold 6430" moved the key and
    orphaned every answer filed against the old one."""
    eng = User.objects.create_user("intel-recycle")
    _member(eng, intel)
    run = ingest.ingest_bundle(
        submitter=eng, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"],
            results=[f.validate_result("validate.cpu.functional")],
        ))),
    )
    before = _claim_key(run)
    _declined(run, intel)
    run.listing_proposal = {"cpu_model": "Intel Xeon Gold 6430"}
    run.save(update_fields=["listing_proposal"])

    assert _claim_key(run) == before, "the key must not move when the model is corrected"

    services.approve_run(release(run), by=reviewer)

    assert _levels(run)["Intel Intel Xeon Scalable 4th Generation"] == (
        ValidationLevel.COMMUNITY
    )


def test_the_correction_still_shows_and_still_ties(intel, reviewer):
    """The other half: keying on the report must not throw the correction away."""
    eng = User.objects.create_user("intel-recycle2")
    _member(eng, intel)
    run = ingest.ingest_bundle(
        submitter=eng, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"],
            results=[f.validate_result("validate.cpu.functional")],
        ))),
    )
    run.component_overrides = {_claim_key(run): {"model": "Xeon Gold 6544Y"}}
    run.save(update_fields=["component_overrides"])

    entry = next(
        e for e in services.preview_component_ties(run) if e["kind"] == "cpu"
    )

    assert entry["raw_model"] == "Xeon Gold 6544Y"
    assert entry["overridden"] is True


def test_a_decline_reaches_the_sibling_runs(intel, reviewer):
    """"Submit all N runs of this machine" is the ordinary multi-release path, and the page says
    the details were applied to the others. The per-component answers were not among them."""
    eng = User.objects.create_user("intel-siblings")
    _member(eng, intel)
    first = ingest.ingest_bundle(
        submitter=eng, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"], version_id="9.6",
            results=[f.validate_result("validate.cpu.functional")],
        ))),
    )
    second = ingest.ingest_bundle(
        submitter=eng, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"], version_id="9.4",
            run_id="cccccccc-0000-0000-0000-000000000001",
            results=[f.validate_result("validate.cpu.functional")],
        ))),
    )
    _declined(first, intel)

    shared = services.share_listing_details(first)

    assert second in shared
    second.refresh_from_db()
    assert second.component_overrides[_claim_key(second)]["attribute_to"] == ""
    services.approve_run(release(second), by=reviewer)
    assert _levels(second)["Intel Intel Xeon Scalable 4th Generation"] == (
        ValidationLevel.COMMUNITY
    )


def test_sharing_does_not_wipe_a_siblings_own_answer(intel, reviewer):
    """Merged per key, not replaced: "applied to your other runs" must not mean one was lost."""
    eng = User.objects.create_user("intel-siblings2")
    _member(eng, intel)
    first = ingest.ingest_bundle(
        submitter=eng, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"], version_id="9.6",
            results=[f.validate_result("validate.cpu.functional")],
        ))),
    )
    second = ingest.ingest_bundle(
        submitter=eng, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"], version_id="9.4",
            run_id="cccccccc-0000-0000-0000-000000000002",
            results=[f.validate_result("validate.cpu.functional")],
        ))),
    )
    board = _claim_key(second, kind="motherboard")
    second.component_overrides = {board: {"brand": "Dell"}}
    second.save(update_fields=["component_overrides"])
    _declined(first, intel)

    services.share_listing_details(first)

    second.refresh_from_db()
    assert second.component_overrides[board]["brand"] == "Dell"
    assert second.component_overrides[_claim_key(second)]["attribute_to"] == ""


def test_a_stored_claim_is_read_as_a_slug_not_a_name(intel, reviewer):
    """The form stores ``vendor.slug``; it was read back with ``resolve_vendor``, which resolves
    *names*. ``slugify`` deletes in-word punctuation that the name normalizer treats as a
    separator, so a vendor's own slug can fail to resolve to it - and an explicit tick then came
    out worse than never saving, because an explicit answer beats the silent default."""
    from lumina.vendors.services import resolve_vendor, vendor_by_slug

    # The same vendor the CPU family belongs to, renamed to a shape that survives slugify badly.
    # Not contrived: "Advanced Micro Devices, Inc. [AMD/ATI]" and "Realtek Semiconductor Co., Ltd."
    # are the names pci.ids ships.
    intel.name = "Intel Corp.,Ltd."
    intel.slug = ""
    intel.save()
    assert intel.slug == "intel-corpltd"
    assert resolve_vendor(intel.slug) is None, "the premise"
    assert vendor_by_slug(intel.slug) == intel

    eng = User.objects.create_user("intel-slug")
    _member(eng, intel)
    run = ingest.ingest_bundle(
        submitter=eng, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"],
            results=[f.validate_result("validate.cpu.functional")],
        ))),
    )
    run.component_overrides = {_claim_key(run): {"attribute_to": intel.slug}}
    run.save(update_fields=["component_overrides"])

    services.approve_run(release(run), by=reviewer)

    assert _levels(run)["Intel Corp.,Ltd. Intel Xeon Scalable 4th Generation"] == (
        ValidationLevel.VENDOR
    )
