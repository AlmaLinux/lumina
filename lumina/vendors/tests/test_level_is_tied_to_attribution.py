"""The vendor tier comes from who a submission is *for*, not from who sent it.

The old rule granted vendor for standing: any `certifier`/`admin` got it
unconditionally, and a vendor member got it whether or not the submission named
their vendor. Two problems fell out of that:

- a run could say "vendor-validated" without saying **which** vendor was doing the
  validating, which is the one thing that claim has to carry; and
- ``effective_level`` fell back to ``listing.owner_vendor``, so a Foundation
  certifier validating a Dell machine was treated as submitting *for Dell* purely
  because Dell owns the listing.

Now:

- naming a vendor **decides** the tier - there is no reason for a run submitted on
  behalf of a vendor to claim anything else, so it is not offered as a choice;
- a plain community member has one option, and the dropdown is dropped entirely;
- `certifier`/`admin` choose between community and almalinux; and
- vendor is never selectable anywhere.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import Group, User

from lumina.core.certification import ValidationLevel
from lumina.vendors.models import Vendor, VendorMembership
from lumina.vendors.services import (
    derive_allowed_levels,
    resolve_claimed_level,
    selectable_levels,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def dell():
    return Vendor.objects.create(name="Dell Inc.", verified=True, published=True)


@pytest.fixture
def unverified():
    return Vendor.objects.create(name="Fly By Night", verified=False, published=True)


@pytest.fixture
def plain():
    return User.objects.create_user("hobbyist")


@pytest.fixture
def member(dell):
    user = User.objects.create_user("dell-eng")
    VendorMembership.objects.create(
        user=user, vendor=dell, role=VendorMembership.ROLE_SUBMITTER
    )
    return user


@pytest.fixture
def certifier():
    user = User.objects.create_user("sig")
    group, _ = Group.objects.get_or_create(name="certifier")
    user.groups.add(group)
    return user


# --- what each person may claim -----------------------------------------------


def test_a_community_member_gets_only_community(plain, dell):
    assert derive_allowed_levels(plain, vendor=None) == [ValidationLevel.COMMUNITY]
    assert derive_allowed_levels(plain, vendor=dell) == [ValidationLevel.COMMUNITY]


def test_a_certifier_gets_almalinux_but_not_vendor(certifier):
    levels = derive_allowed_levels(certifier, vendor=None)

    assert levels == [ValidationLevel.COMMUNITY, ValidationLevel.ALMALINUX]


def test_a_vendor_member_gets_vendor_only_when_the_vendor_is_named(member, dell):
    """Membership alone is not a claim about any particular submission."""
    assert ValidationLevel.VENDOR not in derive_allowed_levels(member, vendor=None)
    assert ValidationLevel.VENDOR in derive_allowed_levels(member, vendor=dell)


def test_a_member_of_one_vendor_cannot_claim_another(member):
    other = Vendor.objects.create(name="Supermicro", verified=True, published=True)

    assert ValidationLevel.VENDOR not in derive_allowed_levels(member, vendor=other)


def test_an_unverified_vendor_confers_nothing(unverified):
    user = User.objects.create_user("eager")
    VendorMembership.objects.create(
        user=user, vendor=unverified, role=VendorMembership.ROLE_SUBMITTER
    )

    assert ValidationLevel.VENDOR not in derive_allowed_levels(
        user, vendor=unverified
    )


def test_vendor_is_the_default_when_it_is_allowed(member, dell):
    """``allowed[-1]`` is what a submission with no explicit claim falls back to."""
    assert derive_allowed_levels(member, vendor=dell)[-1] == ValidationLevel.VENDOR


def test_a_certifier_with_no_vendor_defaults_to_almalinux(certifier):
    """A Foundation certifier validating somebody else's hardware is not that
    vendor, and their run should not say so."""
    assert derive_allowed_levels(certifier, vendor=None)[-1] == (
        ValidationLevel.ALMALINUX
    )


def test_anonymous_submission_is_refused():
    from django.contrib.auth.models import AnonymousUser

    with pytest.raises(PermissionError):
        derive_allowed_levels(AnonymousUser(), vendor=None)


# --- naming a vendor decides the tier -----------------------------------------


def test_submitting_for_a_vendor_is_vendor_validated(member, dell):
    """"There's no reason for it to be anything else." """
    assert resolve_claimed_level(member, vendor=dell) == ValidationLevel.VENDOR


def test_a_lower_claim_alongside_a_vendor_is_overridden(member, dell):
    """So the dropdown never has to offer a choice that contradicts the vendor
    selection sitting next to it."""
    resolved = resolve_claimed_level(
        member, vendor=dell, claimed=ValidationLevel.COMMUNITY
    )

    assert resolved == ValidationLevel.VENDOR


def test_a_certifier_acting_for_a_vendor_is_also_vendor(certifier, dell):
    """Admins and certifiers may act for anyone, so naming a vendor is enough."""
    assert resolve_claimed_level(certifier, vendor=dell) == ValidationLevel.VENDOR


def test_a_claim_beyond_someones_standing_is_capped(plain, dell):
    resolved = resolve_claimed_level(
        plain, vendor=None, claimed=ValidationLevel.ALMALINUX
    )

    assert resolved == ValidationLevel.COMMUNITY


def test_an_entitled_claim_is_honoured(certifier):
    resolved = resolve_claimed_level(
        certifier, vendor=None, claimed=ValidationLevel.COMMUNITY
    )

    assert resolved == ValidationLevel.COMMUNITY


def test_no_claim_falls_back_to_standing(certifier):
    assert resolve_claimed_level(certifier, vendor=None) == ValidationLevel.ALMALINUX


# --- what a dropdown offers ---------------------------------------------------


def test_a_community_member_is_offered_one_thing(plain):
    assert selectable_levels(plain) == [ValidationLevel.COMMUNITY]


def test_a_certifier_is_offered_community_or_almalinux(certifier):
    assert selectable_levels(certifier) == [
        ValidationLevel.COMMUNITY, ValidationLevel.ALMALINUX,
    ]


@pytest.mark.parametrize("who", ["plain", "member", "certifier"])
def test_vendor_is_never_selectable(request, who):
    """Not for anyone, however entitled. It comes from naming the vendor."""
    user = request.getfixturevalue(who)

    assert ValidationLevel.VENDOR not in selectable_levels(user)


def test_the_dropdown_strips_vendor_even_if_the_policy_hands_it_over(
    monkeypatch, certifier
):
    """The guard in ``selectable_levels``, tested for its own sake.

    Passing ``vendor=None`` already means the policy cannot return vendor, so the
    filter is unreachable through the normal path - deleting it leaves every other
    test in this file passing, which is how it was nearly mistaken for dead code.
    It is not: it pins ``selectable_levels``'s contract independently, so a future
    change to ``derive_allowed_levels`` cannot quietly put vendor back in a
    dropdown. This is the only test that can tell.
    """
    monkeypatch.setattr(
        "lumina.vendors.services.derive_allowed_levels",
        lambda user, *, vendor: [
            ValidationLevel.COMMUNITY, ValidationLevel.VENDOR,
            ValidationLevel.ALMALINUX,
        ],
    )

    assert selectable_levels(certifier) == [
        ValidationLevel.COMMUNITY, ValidationLevel.ALMALINUX,
    ]


def test_a_vendor_member_is_not_offered_a_level_choice(member):
    """Their tier follows from the vendor field, so there is nothing to pick."""
    assert selectable_levels(member) == [ValidationLevel.COMMUNITY]


def test_an_anonymous_user_is_offered_community_rather_than_raising(plain):
    from django.contrib.auth.models import AnonymousUser

    assert selectable_levels(AnonymousUser()) == [ValidationLevel.COMMUNITY]
    assert selectable_levels(None) == [ValidationLevel.COMMUNITY]
