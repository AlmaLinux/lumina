"""The real-world software the devstack catalog is seeded with.

These are named companies and named products, so the seed data makes claims about
third parties. The invariants here are what keep those claims honest, and they are
the kind a well-meaning later edit would break without noticing:

- **No real product is vendor-certified.** "Vendor-validated" means the vendor took
  part in the SIG's certification program. AlmaLinux's own site says software
  certification is still in the works, so none of them have. Every real listing
  sits at the community tier.
- **No real vendor is marked verified.** Verification is the SIG vouching that
  somebody represents that company; nobody has claimed any of these.
- **No real listing has an owner_vendor.** Ownership would hand edit rights over a
  real company's listing to whoever happens to hold a membership row.
- Recorded AlmaLinux majors reflect what each vendor documents, which is why Veeam
  stops at 9 and DaVinci Resolve only cites 9.

The invented products (Vaultwise, Meshsight, Orbital Forge) are what exercise the
vendor and AlmaLinux tiers, precisely so the real ones do not have to.
"""
from __future__ import annotations

import pytest

from lumina.core.certification import ValidationLevel
from lumina.core.management.commands.seed_devstack import (
    _ECOSYSTEM_SOFTWARE,
    _ECOSYSTEM_VENDORS,
    _SAMPLE_SOFTWARE_CATEGORIES,
    Command,
)
from lumina.releases.models import AlmaLinuxRelease
from lumina.software.models import Software
from lumina.vendors.models import Vendor

pytestmark = pytest.mark.django_db


@pytest.fixture
def seeded():
    """Just the pieces the ecosystem data needs, so the test stays offline.

    A full ``seed_devstack`` run fetches vendor logos over HTTP.
    """
    for major in (8, 9, 10):
        AlmaLinuxRelease.objects.get_or_create(
            major=major, defaults={"supported": True},
        )
    command = Command()
    command._seed_software_categories()
    command._seed_ecosystem_vendors()
    command._seed_ecosystem_software()
    return command


def test_every_listed_product_is_seeded_and_published(seeded):
    assert Software.objects.filter(published=True).count() == len(
        _ECOSYSTEM_SOFTWARE
    )


def test_no_real_product_claims_a_vendor_or_almalinux_tier(seeded):
    """The central honesty invariant. Breaking it puts a certification in a real
    company's mouth that they never applied for."""
    tiers = set(Software.objects.values_list("validation_level", flat=True))

    assert tiers == {ValidationLevel.COMMUNITY}


def test_no_real_vendor_is_marked_verified(seeded):
    names = [name for name, _homepage in _ECOSYSTEM_VENDORS]
    verified = Vendor.objects.filter(name__in=names, verified=True)

    assert not verified.exists(), list(verified.values_list("name", flat=True))


def test_no_real_listing_is_owned(seeded):
    """Ownership drives edit rights, and nobody from these companies has claimed
    anything. It also keeps every one of them a live target for the claim flow."""
    assert not Software.objects.filter(owner_vendor__isnull=False).exists()


def test_every_category_has_at_least_one_product(seeded):
    """A facet that returns nothing is worse than no facet: it reads as a broken
    filter rather than an empty niche."""
    empty = [
        name for name in _SAMPLE_SOFTWARE_CATEGORIES
        if not Software.objects.filter(category_values__value__value=name).exists()
    ]

    assert empty == []


def test_the_documented_majors_are_what_gets_recorded(seeded):
    """Spot-checks the two entries where the real answer is not "all of them".

    Veeam Agent supports AlmaLinux 8 and 9 and explicitly does not support 10.
    DaVinci Resolve is tested by Blackmagic on Rocky Linux only, so its AlmaLinux
    support is community knowledge about a single major.
    """
    veeam = Software.objects.get(name="Veeam Agent for Linux")
    resolve = Software.objects.get(name="DaVinci Resolve")

    assert sorted(
        row.release.major for row in veeam.compatibility.all()
    ) == [8, 9]
    assert [row.release.major for row in resolve.compatibility.all()] == [9]


def test_every_product_has_at_least_one_confirmation(seeded):
    """A community tier with no attestation behind it is a claim nobody made, and
    ``recompute_levels`` would floor it to community anyway - so the number would
    look earned when it was not."""
    for product in Software.objects.all():
        for row in product.compatibility.all():
            assert row.attestations.exists(), f"{product.name} / {row.release.major}"


def test_seeding_twice_changes_nothing(seeded):
    before = Software.objects.count()
    vendors_before = Vendor.objects.count()

    seeded._seed_ecosystem_vendors()
    seeded._seed_ecosystem_software()

    assert Software.objects.count() == before
    assert Vendor.objects.count() == vendors_before
