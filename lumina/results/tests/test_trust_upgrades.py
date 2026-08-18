"""Trust tier only ever improves, and vendor outranks AlmaLinux.

The scenario driving this: the community validates a system, then the vendor
validates the same system. Approving the vendor's run must raise the listing
from community to vendor-validated, automatically, with nobody passing a
flag.

Adding lower-tier evidence never displaces a higher badge. A tier *can* go down when
the evidence behind it goes away, which is the point of deriving it rather than
storing it - see ``test_per_major_attestation`` and ``test_services``.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import Group, User

from lumina.core.certification import ValidationLevel
from lumina.hardware.models import System
from lumina.results import ingest, services
from lumina.results.tests import factories as f
from lumina.results.tests.helpers import release
from lumina.vendors.models import Vendor, VendorMembership

pytestmark = pytest.mark.django_db


@pytest.fixture
def reviewer():
    user = User.objects.create_user("rev")
    group, _ = Group.objects.get_or_create(name="reviewer")
    user.groups.add(group)
    return user


@pytest.fixture
def dell():
    return Vendor.objects.create(name="Dell Inc.", verified=True, published=True)


@pytest.fixture
def system(dell):
    """Owned by Dell, so Dell members can claim vendor validation on it."""
    return System.objects.create(
        name="PowerEdge R760", vendor=dell, owner_vendor=dell, published=True
    )


def community_user(name="hobbyist"):
    return User.objects.create_user(name)


def vendor_user(dell, name="dell-engineer"):
    user = User.objects.create_user(name)
    VendorMembership.objects.create(
        user=user, vendor=dell, role=VendorMembership.ROLE_SUBMITTER
    )
    return user


def almalinux_user(name="alma-staff"):
    user = User.objects.create_user(name)
    group, _ = Group.objects.get_or_create(name="admin")
    user.groups.add(group)
    return user


def validate(submitter, system, reviewer, run_id=None, claim="", on_behalf_of=None):
    """A passing validation run, approved - the whole trigger for an upgrade.

    ``on_behalf_of`` is how a run says it is the vendor's own validation. It used
    to be inferred from ``listing.owner_vendor``, which conflated "Dell owns this
    listing" with "Dell submitted this run" - so a Foundation certifier validating
    a Dell machine came out vendor-validated. The vendor tier is now tied to
    attribution, and attribution is stated on the submission.
    """
    report = f.make_report(
        run_types=["validate"],
        run_id=run_id,
        results=[f.validate_result("validate.cpu.functional")],
    )
    run = ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(f.build_bundle(report)),
        source="api",
    )
    run.listing_system = system
    run.claimed_validation_level = claim
    run.on_behalf_of = on_behalf_of
    run.save()
    services.approve_run(release(run), by=reviewer)
    system.refresh_from_db()
    return run


# --- the upgrade path ---------------------------------------------------------


def test_community_then_vendor_upgrades_to_vendor(system, dell, reviewer):
    """The headline case, and it needs no flags from either submitter."""
    validate(community_user(), system, reviewer,
             "c0000001-0000-4000-8000-000000000001")
    assert system.validation_level == ValidationLevel.COMMUNITY

    validate(vendor_user(dell), system, reviewer,
             "c0000001-0000-4000-8000-000000000002", on_behalf_of=dell)
    assert system.validation_level == ValidationLevel.VENDOR


def test_community_then_almalinux_upgrades_to_almalinux(system, reviewer):
    validate(community_user(), system, reviewer,
             "c0000002-0000-4000-8000-000000000001")
    validate(almalinux_user(), system, reviewer,
             "c0000002-0000-4000-8000-000000000002")
    assert system.validation_level == ValidationLevel.ALMALINUX


def test_vendor_run_upgrades_without_claiming_anything(system, dell, reviewer):
    """No --claim flag exists on the CLI, so the default has to be right."""
    run = validate(vendor_user(dell), system, reviewer, on_behalf_of=dell)
    assert run.claimed_validation_level == ""      # nothing was claimed
    assert system.validation_level == ValidationLevel.VENDOR


# --- never downgrade ----------------------------------------------------------


def test_community_run_never_downgrades_a_vendor_listing(system, dell, reviewer):
    validate(vendor_user(dell), system, reviewer,
             "c0000003-0000-4000-8000-000000000001", on_behalf_of=dell)
    assert system.validation_level == ValidationLevel.VENDOR

    validate(community_user(), system, reviewer,
             "c0000003-0000-4000-8000-000000000002")
    assert system.validation_level == ValidationLevel.VENDOR


def test_community_run_never_downgrades_an_almalinux_listing(system, reviewer):
    validate(almalinux_user(), system, reviewer,
             "c0000004-0000-4000-8000-000000000001")
    validate(community_user(), system, reviewer,
             "c0000004-0000-4000-8000-000000000002")
    assert system.validation_level == ValidationLevel.ALMALINUX


def test_many_community_runs_never_accumulate_into_an_upgrade(system, dell, reviewer):
    """Attestation count grows; the tier does not. Volume is not authority."""
    validate(vendor_user(dell), system, reviewer,
             "c0000005-0000-4000-8000-000000000001", on_behalf_of=dell)
    for i in range(3):
        validate(community_user(f"hobbyist{i}"), system, reviewer,
                 f"c0000005-0000-4000-8000-00000000000{i + 2}")
    assert system.validation_level == ValidationLevel.VENDOR
    assert system.attestation_count == 4


# --- vendor outranks AlmaLinux -----------------------------------------------


def test_an_almalinux_run_does_not_displace_vendor_validation(system, dell,
                                                              reviewer):
    """Vendor is the higher tier, so AlmaLinux evidence adds to it, not over it.

    The vendor is who has to keep supporting the hardware; AlmaLinux validating it
    as well is a third party vouching, which is worth recording and worth less on a
    one-line badge.

    Nothing is lost either way: both attestations are stored and both tiers stay
    visible per run, which ``test_both_tiers_are_still_recorded_per_run`` pins
    down.
    """
    validate(vendor_user(dell), system, reviewer,
             "c0000006-0000-4000-8000-000000000001", on_behalf_of=dell)
    assert system.validation_level == ValidationLevel.VENDOR

    validate(almalinux_user(), system, reviewer,
             "c0000006-0000-4000-8000-000000000002")
    assert system.validation_level == ValidationLevel.VENDOR


def test_a_vendor_run_overrides_almalinux_validation(system, dell, reviewer):
    """The other direction, and the one that moves: vendor outranks AlmaLinux, so
    the vendor stepping up to certify their own hardware raises the badge."""
    validate(almalinux_user(), system, reviewer,
             "c0000007-0000-4000-8000-000000000001")
    assert system.validation_level == ValidationLevel.ALMALINUX

    validate(vendor_user(dell), system, reviewer,
             "c0000007-0000-4000-8000-000000000002", on_behalf_of=dell)
    assert system.validation_level == ValidationLevel.VENDOR


def test_both_tiers_are_still_recorded_per_run(system, dell, reviewer):
    """The badge stops moving, but who validated stays visible per run."""
    vendor_run = validate(vendor_user(dell), system, reviewer,
                          "c0000008-0000-4000-8000-000000000001", on_behalf_of=dell)
    alma_run = validate(almalinux_user(), system, reviewer,
                        "c0000008-0000-4000-8000-000000000002")
    assert services.run_trust_level(vendor_run, system) == ValidationLevel.VENDOR
    assert services.run_trust_level(alma_run, system) == ValidationLevel.ALMALINUX


# --- entitlement is still the ceiling -----------------------------------------


def test_a_community_member_cannot_claim_vendor(system, reviewer):
    validate(community_user(), system, reviewer, claim=ValidationLevel.VENDOR)
    assert system.validation_level == ValidationLevel.COMMUNITY


def test_vendor_membership_in_another_vendor_does_not_count(system, reviewer):
    """A Dell engineer validating an HP system is just a community member."""
    other = Vendor.objects.create(name="Contoso", verified=True)
    outsider = User.objects.create_user("contoso-eng")
    VendorMembership.objects.create(
        user=outsider, vendor=other, role=VendorMembership.ROLE_SUBMITTER
    )
    validate(outsider, system, reviewer)
    assert system.validation_level == ValidationLevel.COMMUNITY


def test_unverified_vendor_membership_does_not_count(reviewer):
    """Verification is what makes a vendor claim meaningful."""
    unverified = Vendor.objects.create(name="Unchecked Co", verified=False)
    system = System.objects.create(
        name="Mystery Box", vendor=unverified, owner_vendor=unverified,
        published=True,
    )
    validate(vendor_user(unverified, "unchecked-eng"), system, reviewer)
    system.refresh_from_db()
    assert system.validation_level == ValidationLevel.COMMUNITY


def test_a_failing_vendor_run_upgrades_nothing(system, dell, reviewer):
    report = f.make_report(
        run_types=["validate"],
        results=[f.validate_result("validate.cpu.functional", status="fail")],
    )
    run = ingest.ingest_bundle(
        submitter=vendor_user(dell), bundle_file=f.as_upload(f.build_bundle(report)),
        source="api",
    )
    run.listing_system = system
    run.save()
    services.approve_run(release(run), by=reviewer)
    system.refresh_from_db()
    assert system.validation_level == ValidationLevel.COMMUNITY
    assert system.attestation_count == 0
