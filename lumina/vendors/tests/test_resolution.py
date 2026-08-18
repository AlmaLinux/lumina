"""Vendor name resolution: DMI strings vs catalog vendors."""

import pytest

from lumina.vendors.models import Vendor, VendorAlias
from lumina.vendors.services import normalize_vendor_name, resolve_vendor

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Dell, Inc.", "dell"),
        ("Dell Inc.", "dell"),
        ("DELL", "dell"),
        ("ASUSTeK COMPUTER INC.", "asustek"),
        ("Micro-Star International Co., Ltd.", "micro star"),
        ("Hewlett-Packard", "hewlett packard"),
        ("ASRock", "asrock"),
        # a name that is nothing but suffix tokens must not normalize to ""
        ("Company Inc", "company inc"),
    ],
)
def test_normalize_vendor_name(raw, expected):
    assert normalize_vendor_name(raw) == expected


def test_resolve_exact_and_case_insensitive():
    dell = Vendor.objects.create(name="Dell Inc.")
    assert resolve_vendor("Dell Inc.") == dell
    assert resolve_vendor("dell inc.") == dell


def test_resolve_via_normalization():
    """The headline case: DMI says "Dell" but the catalog has "Dell Inc."."""
    dell = Vendor.objects.create(name="Dell Inc.")
    assert resolve_vendor("Dell") == dell
    assert resolve_vendor("Dell, Inc.") == dell


def test_resolve_via_alias():
    msi = Vendor.objects.create(name="MSI")
    VendorAlias.objects.create(vendor=msi, name="Micro-Star International Co., Ltd.")
    assert resolve_vendor("Micro-Star International Co., Ltd.") == msi
    # normalized form of the alias also matches
    assert resolve_vendor("Micro-Star International") == msi


def test_resolve_unknown_returns_none():
    Vendor.objects.create(name="Dell Inc.")
    assert resolve_vendor("Supermicro") is None
    assert resolve_vendor("") is None


# --- merging duplicate vendors -------------------------------------------------


def _dup_pair():
    from lumina.hardware.models import Component, ComponentKind, System

    dell = Vendor.objects.create(name="Dell", verified=True)
    dell_inc = Vendor.objects.create(name="Dell Inc.", homepage="https://dell.com")
    System.objects.create(name="OptiPlex 3080", vendor=dell_inc)
    System.objects.create(name="PowerEdge R750", vendor=dell)
    Component.objects.create(
        name="0M83RH", vendor=dell_inc, kind=ComponentKind.motherboard.value
    )
    return dell, dell_inc


def test_merge_vendors_repoints_everything_and_leaves_alias():
    from lumina.hardware.models import Component, System
    from lumina.vendors.services import merge_vendors

    dell, dell_inc = _dup_pair()
    moved = merge_vendors(dell, dell_inc)

    assert not Vendor.objects.filter(name="Dell Inc.").exists()
    assert System.objects.filter(vendor=dell).count() == 2
    assert Component.objects.filter(vendor=dell).count() == 1
    assert moved["systems"] == 1 and moved["components"] == 1
    # the dead name is an alias now, so resolution can't fork again
    assert resolve_vendor("Dell Inc.") == dell
    # profile gaps filled from the duplicate
    dell.refresh_from_db()
    assert dell.homepage == "https://dell.com"
    assert dell.verified is True


def test_merge_dedupes_memberships():
    from django.contrib.auth.models import User

    from lumina.vendors.models import VendorMembership
    from lumina.vendors.services import merge_vendors

    dell, dell_inc = _dup_pair()
    user = User.objects.create_user("worker")
    VendorMembership.objects.create(user=user, vendor=dell,
                                    role=VendorMembership.ROLE_SUBMITTER)
    VendorMembership.objects.create(user=user, vendor=dell_inc,
                                    role=VendorMembership.ROLE_SUBMITTER)
    merge_vendors(dell, dell_inc)
    assert VendorMembership.objects.filter(user=user).count() == 1
    assert VendorMembership.objects.get(user=user).vendor == dell


def test_merge_refuses_self():
    from lumina.vendors.services import merge_vendors

    dell, _ = _dup_pair()
    with pytest.raises(ValueError):
        merge_vendors(dell, dell)


def test_merge_command_resolves_by_name_or_slug(capsys):
    from django.core.management import call_command

    dell, dell_inc = _dup_pair()
    call_command("merge_vendors", "Dell", "dell-inc")
    assert not Vendor.objects.filter(name="Dell Inc.").exists()
    assert resolve_vendor("Dell Inc.") == dell
