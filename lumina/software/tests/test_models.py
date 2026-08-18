"""The software catalog's data model.

The organising problem is vendor abandonment: a vendor who certifies once and
walks away must not leave a listing that still reads as currently certified. So:

- Validation is per cited AlmaLinux **major**, not per listing.
- A major can hold a vendor certification and an AlmaLinux certification at the
  same time, unlike hardware's single scalar tier.
- Each major carries its own community attestation count, one per user.
- The listing badge is the highest tier across its majors (the user's choice over
  newest-cited-major). The per-major detail is read on the detail page.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction

from lumina.core.certification import ValidationLevel
from lumina.releases.models import AlmaLinuxRelease
from lumina.software.models import (
    Software,
    SoftwareAttestation,
    SoftwareCertification,
    SoftwareCompatibility,
)
from lumina.vendors.models import Vendor

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture(autouse=True)
def releases():
    for major in (8, 9, 10):
        AlmaLinuxRelease.objects.get_or_create(
            major=major, defaults={"supported": True}
        )


@pytest.fixture
def acme():
    return Vendor.objects.create(name="Acme Software", scope=Vendor.SCOPE_SOFTWARE)


@pytest.fixture
def backup(acme):
    return Software.objects.create(
        vendor=acme, name="Acme Backup", published=True,
    )


def _major(software, major):
    return SoftwareCompatibility.objects.create(
        software=software, release=AlmaLinuxRelease.objects.get(major=major),
    )


# --- per-major validation -----------------------------------------------------


def test_a_major_with_nothing_backing_it_reads_as_community(backup):
    """The floor, not a claim: a row exists because somebody cited it."""
    row = _major(backup, 9)

    assert row.validation_level == ValidationLevel.COMMUNITY


def test_a_vendor_certification_lifts_only_its_own_major(backup):
    """This is the whole anti-abandonment mechanism in one assertion."""
    nine = _major(backup, 9)
    ten = _major(backup, 10)
    SoftwareCertification.objects.create(
        compatibility=nine, level=ValidationLevel.VENDOR,
    )

    nine.refresh_from_db()
    ten.refresh_from_db()
    assert nine.validation_level == ValidationLevel.VENDOR
    assert ten.validation_level == ValidationLevel.COMMUNITY


def test_one_major_can_hold_both_vendor_and_almalinux_certifications(backup):
    """Both tiers coexist on one major, and vendor is the one the badge shows."""
    nine = _major(backup, 9)
    SoftwareCertification.objects.create(compatibility=nine,
                                         level=ValidationLevel.VENDOR)
    SoftwareCertification.objects.create(compatibility=nine,
                                         level=ValidationLevel.ALMALINUX)

    assert nine.certifications.count() == 2
    assert {c.level for c in nine.certifications.all()} == {"vendor", "almalinux"}


def test_vendor_outranks_almalinux_on_a_major_holding_both(backup):
    """The tier ordering, on the one shape that can distinguish the two rules.

    ``derived_level`` used to hand-roll a ranking that returned AlmaLinux here,
    left over from when the tiers shared rank 1. Every other shape agrees with
    ``highest_level``, so this row is the only test that can tell them apart - and
    it existed already, asserting only the certification set.
    """
    nine = _major(backup, 9)
    for level in (ValidationLevel.ALMALINUX, ValidationLevel.VENDOR):
        SoftwareCertification.objects.create(compatibility=nine, level=level)

    assert nine.derived_level() == ValidationLevel.VENDOR

    backup.recompute_levels()
    nine.refresh_from_db()
    backup.refresh_from_db()
    assert nine.validation_level == ValidationLevel.VENDOR, "wrong value persisted"
    assert backup.validation_level == ValidationLevel.VENDOR, "and reached the badge"


def test_the_same_validator_cannot_certify_one_major_twice(backup):
    nine = _major(backup, 9)
    SoftwareCertification.objects.create(compatibility=nine,
                                         level=ValidationLevel.VENDOR)

    with pytest.raises(IntegrityError), transaction.atomic():
        SoftwareCertification.objects.create(compatibility=nine,
                                             level=ValidationLevel.VENDOR)


def test_a_major_is_cited_at_most_once_per_product(backup):
    _major(backup, 9)

    with pytest.raises(IntegrityError), transaction.atomic():
        _major(backup, 9)


# --- the listing badge --------------------------------------------------------


def test_the_listing_badge_is_the_highest_tier_across_majors(backup):
    """The user chose highest-across-all over newest-cited. A vendor who
    certified 9 and never touched 10 therefore keeps a Vendor-validated card, and
    the decay is legible on the detail page's per-major table rather than on the
    card."""
    nine = _major(backup, 9)
    _major(backup, 10)
    SoftwareCertification.objects.create(compatibility=nine,
                                         level=ValidationLevel.VENDOR)

    backup.refresh_from_db()
    assert backup.validation_level == ValidationLevel.VENDOR


def test_a_pending_major_does_not_lift_the_listing_badge(backup):
    """A community report awaiting review must not promote the product."""
    pending = _major(backup, 10)
    pending.status = SoftwareCompatibility.STATUS_PENDING
    pending.save(update_fields=["status"])
    SoftwareCertification.objects.create(compatibility=pending,
                                         level=ValidationLevel.ALMALINUX)

    backup.refresh_from_db()
    assert backup.validation_level == ValidationLevel.COMMUNITY


def test_removing_a_certification_lowers_the_badge_again(backup):
    """Deliberately unlike hardware's sticky upgrade_level_if_higher: the tier
    is derived from a certification set, so losing one is a real downgrade."""
    nine = _major(backup, 9)
    cert = SoftwareCertification.objects.create(compatibility=nine,
                                               level=ValidationLevel.VENDOR)
    backup.refresh_from_db()
    assert backup.validation_level == ValidationLevel.VENDOR

    cert.delete()

    backup.refresh_from_db()
    nine.refresh_from_db()
    assert nine.validation_level == ValidationLevel.COMMUNITY
    assert backup.validation_level == ValidationLevel.COMMUNITY


# --- attestations -------------------------------------------------------------


def test_a_user_may_confirm_each_major_once(backup):
    user = User.objects.create_user("fan", email="f@example.com")
    nine, ten = _major(backup, 9), _major(backup, 10)

    SoftwareAttestation.objects.create(compatibility=nine, user=user)
    SoftwareAttestation.objects.create(compatibility=ten, user=user)

    assert SoftwareAttestation.objects.filter(user=user).count() == 2


def test_the_same_user_cannot_confirm_one_major_twice(backup):
    user = User.objects.create_user("fan", email="f@example.com")
    nine = _major(backup, 9)
    SoftwareAttestation.objects.create(compatibility=nine, user=user)

    with pytest.raises(IntegrityError), transaction.atomic():
        SoftwareAttestation.objects.create(compatibility=nine, user=user)


def test_a_certified_major_still_shows_its_community_confirmations(backup):
    """The community got there first and the page should say so."""
    nine = _major(backup, 9)
    SoftwareCertification.objects.create(compatibility=nine,
                                         level=ValidationLevel.VENDOR)
    for i in range(3):
        SoftwareAttestation.objects.create(
            compatibility=nine,
            user=User.objects.create_user(f"u{i}", email=f"u{i}@example.com"),
        )

    nine.refresh_from_db()
    assert nine.validation_level == ValidationLevel.VENDOR
    assert nine.attestations.count() == 3


# --- querysets ----------------------------------------------------------------


def test_pending_majors_are_excluded_from_the_approved_queryset(backup):
    _major(backup, 9)
    pending = _major(backup, 10)
    pending.status = SoftwareCompatibility.STATUS_PENDING
    pending.save(update_fields=["status"])

    assert SoftwareCompatibility.objects.approved().count() == 1
    assert SoftwareCompatibility.objects.pending().count() == 1


def test_proposing_a_major_creates_it_pending(backup):
    reporter = User.objects.create_user("rep", email="rep@example.com")

    row = SoftwareCompatibility.propose(
        software=backup,
        release=AlmaLinuxRelease.objects.get(major=10),
        proposed_by=reporter,
    )

    assert row.status == SoftwareCompatibility.STATUS_PENDING
    assert row.proposed_by == reporter
