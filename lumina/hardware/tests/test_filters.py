"""Tests for catalog filtering.

The public browse pages compose filters from query-string parameters:

- ``vendor=<slug>`` (repeatable) - OR across values within this filter.
- ``<category-slug>=<value-slug>`` (repeatable) - OR across values within a
  category; AND across categories. This mirrors catalog.redhat.com's
  pre-fill-a-selector pattern.

Unpublished listings are never returned. Category values in ``pending``
or ``rejected`` status are never matched.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from lumina.hardware.filters import filter_listings
from lumina.hardware.models import (
    Component,
    ListingCategoryValue,
    System,
)
from lumina.taxonomy.models import Category, CategoryValue
from lumina.vendors.models import Vendor

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture
def dell():
    return Vendor.objects.create(name="Dell")


@pytest.fixture
def hpe():
    return Vendor.objects.create(name="HPE")


@pytest.fixture
def arch():
    return Category.objects.create(name="Architecture", slug="architecture")


@pytest.fixture
def x86(arch):
    return CategoryValue.objects.create(category=arch, value="x86_64")


@pytest.fixture
def arm(arch):
    return CategoryValue.objects.create(category=arch, value="aarch64")


def _published_system(name: str, vendor: Vendor, **kwargs) -> System:
    s = System.objects.create(name=name, vendor=vendor, model_number=name, **kwargs)
    s.published = True
    s.save()
    return s


def _tag(system: System, value: CategoryValue) -> None:
    ListingCategoryValue.objects.create(listing_system=system, value=value)


class BasePublishedFilterTests:
    def test_only_published_systems_returned(self, dell):
        draft = System.objects.create(name="Draft", vendor=dell, model_number="x")
        published = _published_system("Live", dell)
        qs = filter_listings(System, params={})
        assert published in qs
        assert draft not in qs


class VendorFilterTests:
    def test_single_vendor(self, dell, hpe):
        a = _published_system("A", dell)
        _published_system("B", hpe)
        qs = filter_listings(System, params={"vendor": ["dell"]})
        assert list(qs) == [a]

    def test_multiple_vendors_or_combined(self, dell, hpe):
        a = _published_system("A", dell)
        b = _published_system("B", hpe)
        qs = filter_listings(System, params={"vendor": ["dell", "hpe"]})
        assert set(qs) == {a, b}

    def test_unknown_vendor_slug_returns_empty(self, dell):
        _published_system("A", dell)
        qs = filter_listings(System, params={"vendor": ["nope"]})
        assert list(qs) == []


class CategoryFilterTests:
    def test_single_value(self, dell, x86, arm):
        a = _published_system("A", dell)
        b = _published_system("B", dell)
        _tag(a, x86)
        _tag(b, arm)
        qs = filter_listings(System, params={"architecture": ["x86_64"]})
        assert set(qs) == {a}

    def test_multiple_values_in_same_category_or_combined(self, dell, x86, arm):
        a = _published_system("A", dell)
        b = _published_system("B", dell)
        _tag(a, x86)
        _tag(b, arm)
        qs = filter_listings(System, params={"architecture": ["x86_64", "aarch64"]})
        assert set(qs) == {a, b}

    def test_different_categories_and_combined(self, dell, arch, x86):
        network = Category.objects.create(name="Network", slug="network")
        gbe = CategoryValue.objects.create(category=network, value="1gbe")
        tengbe = CategoryValue.objects.create(category=network, value="10gbe")

        a = _published_system("A", dell)
        b = _published_system("B", dell)
        _tag(a, x86)
        _tag(a, gbe)
        _tag(b, x86)
        _tag(b, tengbe)

        qs = filter_listings(
            System,
            params={"architecture": ["x86_64"], "network": ["10gbe"]},
        )
        assert set(qs) == {b}

    def test_pending_values_do_not_match(self, dell, arch):
        submitter = User.objects.create_user(username="p")
        pending = CategoryValue.propose(category=arch, value="riscv64", proposed_by=submitter)
        a = _published_system("A", dell)
        # Bind the listing to a pending value (possible only transiently on
        # an unpublished draft; we test that the filter still ignores it).
        ListingCategoryValue.objects.create(listing_system=a, value=pending)
        qs = filter_listings(System, params={"architecture": ["riscv64"]})
        assert list(qs) == []


class AlmaLinuxReleaseFilterTests:
    def test_alma_param_filters_by_listing_versions(self, dell):
        from lumina.hardware.models import ListingVersion
        from lumina.releases.models import AlmaLinuxRelease

        alma9 = AlmaLinuxRelease.objects.create(major=9)
        alma10 = AlmaLinuxRelease.objects.create(major=10)
        a = _published_system("A", dell)
        b = _published_system("B", dell)
        ListingVersion.objects.create(listing_system=a, release=alma9)
        ListingVersion.objects.create(listing_system=b, release=alma10)

        qs = filter_listings(System, params={"alma": ["9"]})
        assert set(qs) == {a}
        qs = filter_listings(System, params={"alma": ["9", "10"]})
        assert set(qs) == {a, b}

    def test_non_integer_alma_param_ignored(self, dell):
        a = _published_system("A", dell)
        # Junk value shouldn't blow up or accidentally exclude everything.
        qs = filter_listings(System, params={"alma": ["nope"]})
        assert a in qs


class FreeTextSearchTests:
    def test_q_matches_name(self, dell):
        a = _published_system("PowerEdge R750", dell)
        _published_system("ProLiant DL380", dell)
        qs = filter_listings(System, params={"q": ["R750"]})
        assert set(qs) == {a}

    def test_q_matches_model_number_case_insensitive(self, dell):
        a = _published_system("System A", dell)
        # Model numbers often differ from names; search should cover both.
        a.model_number = "BCM57414"
        a.save()
        qs = filter_listings(System, params={"q": ["bcm574"]})
        assert set(qs) == {a}

    def test_q_matches_vendor_name(self, dell, hpe):
        a = _published_system("A", dell)
        _published_system("B", hpe)
        qs = filter_listings(System, params={"q": ["Dell"]})
        assert set(qs) == {a}

    def test_empty_q_is_ignored(self, dell):
        a = _published_system("A", dell)
        qs = filter_listings(System, params={"q": [""]})
        assert set(qs) == {a}


class ComponentFilterTests:
    def test_component_model_is_filtered_independently(self, dell, x86):
        sys_a = _published_system("SysA", dell)
        _tag(sys_a, x86)
        comp = Component.objects.create(
            name="NIC", vendor=dell, model_number="BCM57414"
        )
        comp.published = True
        comp.save()
        ListingCategoryValue.objects.create(listing_component=comp, value=x86)

        sys_qs = filter_listings(System, params={"architecture": ["x86_64"]})
        comp_qs = filter_listings(Component, params={"architecture": ["x86_64"]})
        assert sys_a in sys_qs and comp not in sys_qs
        assert comp in comp_qs and sys_a not in comp_qs


class KindFilterTests:
    def test_kind_filters_components(self, dell):
        from lumina.hardware.models import Component, ComponentKind

        board = Component.objects.create(
            name="B650M PG Riptide", vendor=dell, published=True,
            kind=ComponentKind.motherboard.value,
        )
        gpu = Component.objects.create(
            name="GeForce RTX 4090", vendor=dell, published=True,
            kind=ComponentKind.gpu.value,
        )
        qs = filter_listings(Component, params={"kind": ["motherboard"]})
        assert set(qs) == {board}
        # multi-select ORs
        qs = filter_listings(Component, params={"kind": ["motherboard", "gpu"]})
        assert set(qs) == {board, gpu}

    def test_invalid_kind_values_are_ignored(self, dell):
        from lumina.hardware.models import Component, ComponentKind

        board = Component.objects.create(
            name="X670E", vendor=dell, published=True,
            kind=ComponentKind.motherboard.value,
        )
        qs = filter_listings(Component, params={"kind": ["bogus"]})
        assert set(qs) == {board}  # nothing valid selected -> unfiltered

    def test_kind_param_ignored_for_systems(self, dell):
        system = _published_system("R760", dell)
        qs = filter_listings(System, params={"kind": ["motherboard"]})
        assert set(qs) == {system}
