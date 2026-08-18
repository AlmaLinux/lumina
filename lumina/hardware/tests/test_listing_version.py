"""Tests for ListingVersion - the per-listing AlmaLinux compatibility row.

A ListingVersion records that a given listing supports an AlmaLinux major
release, optionally with a minimum minor version (kernel-level granularity).
Examples:

- (PowerEdge R750, AlmaLinux 9, 4) → "supported on AlmaLinux 9.4+"
- (PowerEdge R750, AlmaLinux 10, 0) → "supported on AlmaLinux 10.0+"

Exactly one of ``listing_system`` / ``listing_component`` is set, mirroring
the ListingCategoryValue constraint shape.
"""
from __future__ import annotations

import pytest
from django.db import IntegrityError

from lumina.hardware.models import Component, ListingVersion, System
from lumina.releases.models import AlmaLinuxRelease
from lumina.vendors.models import Vendor

pytestmark = pytest.mark.django_db


@pytest.fixture
def dell():
    return Vendor.objects.create(name="Dell")


@pytest.fixture
def system(dell):
    return System.objects.create(name="PowerEdge R750", vendor=dell, model_number="R750")


@pytest.fixture
def alma10():
    return AlmaLinuxRelease.objects.create(major=10)


@pytest.fixture
def alma9():
    return AlmaLinuxRelease.objects.create(major=9)


class ListingVersionTests:
    def test_attach_to_system(self, system, alma10):
        v = ListingVersion.objects.create(listing_system=system, release=alma10)
        assert v in system.versions.all()

    def test_attach_to_component(self, dell, alma10):
        c = Component.objects.create(name="NIC", vendor=dell, model_number="x")
        v = ListingVersion.objects.create(listing_component=c, release=alma10)
        assert v in c.versions.all()

    def test_display_names_the_major(self, system, alma10):
        """One label, because a row is one major.

        There were two tests here: a row with a floor rendered "AlmaLinux 10.4+" and one
        without rendered "AlmaLinux 10". Certification is per major, so only the second
        spelling remains and the run's minor lives on the run.
        """
        v = ListingVersion.objects.create(listing_system=system, release=alma10)
        assert v.display == "AlmaLinux 10"

    def test_unique_per_system_release(self, system, alma10):
        ListingVersion.objects.create(listing_system=system, release=alma10)
        with pytest.raises(IntegrityError):
            ListingVersion.objects.create(listing_system=system, release=alma10)

    def test_same_release_on_different_listings_ok(self, system, dell, alma10):
        other = System.objects.create(name="Other", vendor=dell, model_number="O")
        ListingVersion.objects.create(listing_system=system, release=alma10)
        # Should not raise.
        ListingVersion.objects.create(listing_system=other, release=alma10)
