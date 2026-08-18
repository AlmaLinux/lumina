"""Software browse filtering.

Shares ``apply_category_filters`` with the hardware catalog so the two cannot
drift, and is itself shared between the HTML view and the API viewset for the
same reason.

The two software-specific rules:
- ``?alma=`` matches only **approved** majors, so a pending community report does
  not make a product show up under a release it is not yet cited for.
"""
from __future__ import annotations

import pytest

from lumina.releases.models import AlmaLinuxRelease
from lumina.software.filters import filter_software
from lumina.software.models import Software, SoftwareCategoryValue, SoftwareCompatibility
from lumina.taxonomy.models import Category, CategoryValue
from lumina.vendors.models import Vendor

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def releases():
    for major in (8, 9, 10):
        AlmaLinuxRelease.objects.get_or_create(major=major,
                                               defaults={"supported": True})


@pytest.fixture
def backup_category():
    category = Category.objects.create(
        name="Backup", slug="backup", applies_to=Category.APPLIES_SOFTWARE,
    )
    return CategoryValue.objects.create(category=category, value="Backup")


def _product(name, *, vendor=None, published=True, majors=(9,),
             pending_majors=()):
    vendor = vendor or Vendor.objects.create(
        name=f"V-{name}", scope=Vendor.SCOPE_SOFTWARE,
    )
    product = Software.objects.create(
        vendor=vendor, name=name, published=published,
    )
    for major in majors:
        SoftwareCompatibility.objects.create(
            software=product, release=AlmaLinuxRelease.objects.get(major=major),
        )
    for major in pending_majors:
        SoftwareCompatibility.objects.create(
            software=product, release=AlmaLinuxRelease.objects.get(major=major),
            status=SoftwareCompatibility.STATUS_PENDING,
        )
    return product


def _names(qs):
    return {p.name for p in qs}


# --- visibility ---------------------------------------------------------------


def test_unpublished_software_is_never_returned():
    _product("Draft", published=False)
    _product("Live")

    assert _names(filter_software(params={})) == {"Live"}


# --- release filter -----------------------------------------------------------


def test_filtering_by_major_matches_cited_majors():
    _product("Nines", majors=(9,))
    _product("Tens", majors=(10,))

    assert _names(filter_software(params={"alma": ["9"]})) == {"Nines"}


def test_several_majors_are_ored():
    _product("Nines", majors=(9,))
    _product("Tens", majors=(10,))
    _product("Eights", majors=(8,))

    assert _names(filter_software(params={"alma": ["9", "10"]})) == {"Nines", "Tens"}


def test_a_pending_major_does_not_make_a_product_match_it():
    """A community report awaiting review must not put the product under that
    release before a reviewer has accepted it."""
    _product("Reported", majors=(9,), pending_majors=(10,))

    assert _names(filter_software(params={"alma": ["10"]})) == set()
    assert _names(filter_software(params={"alma": ["9"]})) == {"Reported"}


def test_a_nonsense_major_is_ignored_rather_than_crashing():
    _product("Nines", majors=(9,))

    assert _names(filter_software(params={"alma": ["not-a-number"]})) == {"Nines"}




# --- search and vendor --------------------------------------------------------


def test_search_matches_name_and_vendor():
    vendor = Vendor.objects.create(name="Vaultwise", scope=Vendor.SCOPE_SOFTWARE)
    _product("Archive", vendor=vendor)
    _product("Unrelated")

    assert _names(filter_software(params={"q": ["vaultwise"]})) == {"Archive"}
    assert _names(filter_software(params={"q": ["archi"]})) == {"Archive"}


def test_a_blank_search_does_not_empty_the_results():
    _product("Archive")

    assert _names(filter_software(params={"q": [""]})) == {"Archive"}


def test_filtering_by_vendor_slug():
    vendor = Vendor.objects.create(name="Vaultwise", scope=Vendor.SCOPE_SOFTWARE)
    _product("Archive", vendor=vendor)
    _product("Other")

    assert _names(filter_software(params={"vendor": [vendor.slug]})) == {"Archive"}


# --- categories, via the shared taxonomy helper -------------------------------


def test_filtering_by_category(backup_category):
    tagged = _product("Tagged")
    SoftwareCategoryValue.objects.create(software=tagged, value=backup_category)
    _product("Untagged")

    result = filter_software(params={"backup": [backup_category.slug]})

    assert _names(result) == {"Tagged"}


def test_a_pending_category_value_never_matches(backup_category):
    """Same rule the hardware catalog enforces, inherited from the shared helper."""
    backup_category.status = CategoryValue.STATUS_PENDING
    backup_category.save(update_fields=["status"])
    tagged = _product("Tagged")
    SoftwareCategoryValue.objects.create(software=tagged, value=backup_category)

    assert _names(filter_software(params={"backup": [backup_category.slug]})) == set()


def test_a_typo_in_a_category_param_is_ignored(backup_category):
    _product("Anything")

    assert _names(filter_software(params={"nonsense": ["x"]})) == {"Anything"}


def test_each_product_appears_once_despite_multiple_majors():
    """A join across three majors would otherwise triple the row."""
    _product("Triple", majors=(8, 9, 10))

    assert len(list(filter_software(params={"alma": ["8", "9", "10"]}))) == 1
