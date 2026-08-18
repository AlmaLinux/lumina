"""Per-major validation levels and attestations on hardware listings.

Hardware certified a listing as a whole: one level, one count, and
``upgrade_level_if_higher`` that never went down. Compatibility was already
tracked per major in ``ListingVersion``, but those rows carried no level and no
evidence, so they were labels rather than units of validation.

The case that forced the change: a community member proves an older machine still
works on a new AlmaLinux. A vendor certifies a server on 8, walks away, 10 ships,
someone with that hardware runs the suite on 10.

That evidence used to be **thrown away**. ``_attest_one`` deduped on
``(submitter, listing)``, so anyone who had ever validated the machine before got
no attestation at all for their new-major run. These tests pin the new rule: one
counted attestation per person **per major**.

Hardware attests by running the suite, not by clicking. There is no thumbs-up
here and no separate review step for a new major, because the run itself already
passes reviewer approval.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import Group, User

from lumina.core.certification import ValidationLevel
from lumina.hardware.models import CommunityAttestation, ListingVersion, System
from lumina.hardware.services import recompute_listing_levels
from lumina.results import ingest, services
from lumina.results.tests import factories as f
from lumina.results.tests.helpers import release
from lumina.vendors.models import Vendor, VendorMembership

pytestmark = pytest.mark.django_db


@pytest.fixture
def reviewer():
    user = User.objects.create_user("reviewer")
    user.groups.add(Group.objects.get_or_create(name="reviewer")[0])
    return user


@pytest.fixture
def vendor():
    return Vendor.objects.create(name="ACME", verified=True, published=True)


@pytest.fixture
def system(vendor):
    return System.objects.create(name="R760", vendor=vendor, owner_vendor=vendor)


def _approved_run_on(submitter, system, reviewer, *, version_id="9.6", level="",
                     on_behalf_of=None):
    """Ingest, release, and approve one passing validation run on a given major.

    ``on_behalf_of`` is what makes a run the vendor's own validation. The tier used
    to be inferred from ``listing.owner_vendor``, which said "Dell owns this
    listing" and was read as "Dell submitted this run"; it is now tied to the
    attribution the submission actually states.
    """
    report = f.make_report(
        run_types=["validate"],
        results=[f.validate_result("validate.cpu.functional")],
        version_id=version_id,
    )
    run = ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(f.build_bundle(report)),
        source="api",
    )
    run.listing_system = system
    run.claimed_validation_level = level
    run.on_behalf_of = on_behalf_of
    run.save(update_fields=["listing_system", "claimed_validation_level",
                            "on_behalf_of"])
    services.approve_run(release(run, submitter), by=reviewer)
    return run


def _versions(listing) -> dict[int, ListingVersion]:
    return {v.release.major: v for v in listing.versions.select_related("release")}


def _counts(listing) -> dict[int, int]:
    return {
        major: v.attestations.count() for major, v in _versions(listing).items()
    }


# --- the rule that changed ---------------------------------------------------


def test_a_second_major_from_the_same_person_counts(system, reviewer):
    """The reported bug. One person, two majors, two attestations.

    Under the old per-listing dedup the 10 run produced nothing at all: the
    person had already attested the listing, so their proof that it works on a
    newer AlmaLinux was discarded.
    """
    alice = User.objects.create_user("alice")
    _approved_run_on(alice, system, reviewer, version_id="9.6")
    _approved_run_on(alice, system, reviewer, version_id="10.2")

    assert _counts(system) == {9: 1, 10: 1}
    assert CommunityAttestation.objects.filter(listing_system=system).count() == 2


def test_a_repeat_on_the_same_major_does_not_count_twice(system, reviewer):
    """Still one per person per major. Running the suite twice on 9 is one
    confirmation, which is what the old rule got right."""
    alice = User.objects.create_user("alice")
    _approved_run_on(alice, system, reviewer, version_id="9.6")
    _approved_run_on(alice, system, reviewer, version_id="9.4")

    assert _counts(system) == {9: 1}


def test_two_people_on_one_major_both_count(system, reviewer):
    for name in ("alice", "bob"):
        _approved_run_on(
            User.objects.create_user(name), system, reviewer, version_id="10.2",
        )

    assert _counts(system) == {10: 2}


def test_all_runs_are_kept_even_when_only_one_attestation_counts(system, reviewer):
    """"Keeping all validation runs, only counting one per person per major.\""""
    alice = User.objects.create_user("alice")
    first = _approved_run_on(alice, system, reviewer, version_id="9.6")
    second = _approved_run_on(alice, system, reviewer, version_id="9.4")

    assert first.pk != second.pk
    assert _counts(system) == {9: 1}
    # Both runs survive as evidence.
    assert system.test_runs.count() == 2


# --- per-major levels --------------------------------------------------------


def test_each_major_carries_its_own_level(system, vendor, reviewer):
    engineer = User.objects.create_user("acme-eng")
    VendorMembership.objects.create(
        user=engineer, vendor=vendor, role=VendorMembership.ROLE_SUBMITTER,
    )
    _approved_run_on(engineer, system, reviewer, version_id="9.6",
                     level=ValidationLevel.VENDOR, on_behalf_of=vendor)
    _approved_run_on(User.objects.create_user("fan"), system, reviewer,
                     version_id="10.2")

    versions = _versions(system)
    assert versions[9].validation_level == ValidationLevel.VENDOR
    assert versions[10].validation_level == ValidationLevel.COMMUNITY


def test_the_listing_badge_is_the_highest_across_majors(system, vendor, reviewer):
    """Chosen over newest-cited-major, so a vendor-validated 8 keeps the badge
    while the per-major table carries the detail."""
    engineer = User.objects.create_user("acme-eng")
    VendorMembership.objects.create(
        user=engineer, vendor=vendor, role=VendorMembership.ROLE_SUBMITTER,
    )
    _approved_run_on(engineer, system, reviewer, version_id="8.10",
                     level=ValidationLevel.VENDOR, on_behalf_of=vendor)
    _approved_run_on(User.objects.create_user("fan"), system, reviewer,
                     version_id="10.2")

    system.refresh_from_db()
    assert system.validation_level == ValidationLevel.VENDOR


def test_the_listing_count_totals_the_majors(system, reviewer):
    alice = User.objects.create_user("alice")
    _approved_run_on(alice, system, reviewer, version_id="9.6")
    _approved_run_on(alice, system, reviewer, version_id="10.2")
    _approved_run_on(User.objects.create_user("bob"), system, reviewer,
                     version_id="10.2")

    system.refresh_from_db()
    # Total across majors, not distinct people: alice contributed twice.
    assert system.attestation_count == 3


def test_a_declared_major_has_no_level(system, reviewer):
    """Hardware distinguishes proven from declared and software does not.

    A row somebody typed in has no evidence behind it, so it carries no tier
    rather than being floored at community - that would claim trust nothing
    earned.
    """
    from lumina.releases.models import AlmaLinuxRelease

    declared = ListingVersion.objects.create(
        listing_system=system,
        release=AlmaLinuxRelease.objects.get(major=8),
        source=ListingVersion.SOURCE_DECLARED,
    )
    _approved_run_on(User.objects.create_user("fan"), system, reviewer,
                     version_id="10.2")

    declared.refresh_from_db()
    assert declared.validation_level == ""
    assert _versions(system)[10].validation_level == ValidationLevel.COMMUNITY


def test_losing_an_attestation_lowers_that_majors_level(system, vendor, reviewer):
    """Replaces sticky ``upgrade_level_if_higher``. A level is derived, so
    removing the evidence behind it removes the claim."""
    engineer = User.objects.create_user("acme-eng")
    VendorMembership.objects.create(
        user=engineer, vendor=vendor, role=VendorMembership.ROLE_SUBMITTER,
    )
    _approved_run_on(engineer, system, reviewer, version_id="9.6",
                     level=ValidationLevel.VENDOR, on_behalf_of=vendor)
    _approved_run_on(User.objects.create_user("fan"), system, reviewer,
                     version_id="9.4")
    assert _versions(system)[9].validation_level == ValidationLevel.VENDOR

    CommunityAttestation.objects.filter(
        attested_by=engineer, version__listing_system=system
    ).delete()
    recompute_listing_levels(system)

    assert _versions(system)[9].validation_level == ValidationLevel.COMMUNITY
    system.refresh_from_db()
    assert system.validation_level == ValidationLevel.COMMUNITY


# --- the scenario this was built for -----------------------------------------


def test_community_proves_a_new_major_on_abandoned_hardware(system, vendor, reviewer):
    """Vendor certifies 8 and stops. AlmaLinux 10 ships. The community proves it.

    No new workflow: running the suite creates the 10 row, and the run's own
    review is the review.
    """
    engineer = User.objects.create_user("acme-eng")
    VendorMembership.objects.create(
        user=engineer, vendor=vendor, role=VendorMembership.ROLE_SUBMITTER,
    )
    _approved_run_on(engineer, system, reviewer, version_id="8.10",
                     level=ValidationLevel.VENDOR, on_behalf_of=vendor)
    assert set(_versions(system)) == {8}

    _approved_run_on(User.objects.create_user("fan"), system, reviewer,
                     version_id="10.2")

    versions = _versions(system)
    assert set(versions) == {8, 10}
    # The vendor's own row is untouched by community evidence on another major.
    assert versions[8].validation_level == ValidationLevel.VENDOR
    assert versions[10].validation_level == ValidationLevel.COMMUNITY
    assert versions[10].source == ListingVersion.SOURCE_RUN
    # Evidence-shaped floor, unchanged by this work.
    assert versions[10].source == "run"


def test_the_version_row_exists_before_its_attestation(system, reviewer):
    """Ordering pin.

    ``approve_run`` used to call ``_apply_attestation`` before
    ``record_compatibility``. With attestations hanging off the version row, that
    order silently attests nothing on a listing's first-ever run.
    """
    _approved_run_on(User.objects.create_user("first"), system, reviewer,
                     version_id="10.2")

    assert _counts(system) == {10: 1}


def test_a_run_on_an_unknown_major_attests_nothing(system, reviewer):
    """No AlmaLinuxRelease row means we cannot say which major was proven.

    Narrower than before, when such a run still lifted the listing's level. The
    honest outcome: nothing is certified until an admin creates the release.
    """
    _approved_run_on(User.objects.create_user("fan"), system, reviewer,
                     version_id="42.0")

    assert system.versions.count() == 0
    assert CommunityAttestation.objects.filter(listing_system=system).count() == 0
