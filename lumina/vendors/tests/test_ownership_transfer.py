"""Ownership transfer, and the single enumeration of owned-listing models.

- ``transfer_unowned_listings`` hands a vendor's ownerless listings to it, which
  is what makes a claim useful: the claimant gets edit rights over everything
  already attributed to them.
- It must never steal a listing another vendor already owns.
- ``OWNED_LISTING_MODELS`` is the one list of models carrying both a ``vendor``
  and an ``owner_vendor`` FK. ``merge_vendors`` used to hardcode its own copy,
  which is how a new listing type would silently keep orphan FKs through a
  merge, so both functions must read the same constant.
"""
from __future__ import annotations

import pytest
from django.apps import apps
from django.contrib.auth import get_user_model

from lumina.hardware.models import Component, ComponentKind, System
from lumina.vendors.models import Vendor
from lumina.vendors.services import (
    OWNED_LISTING_MODELS,
    merge_vendors,
    transfer_unowned_listings,
)

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture
def acme():
    return Vendor.objects.create(name="Acme Corp", verified=True)


@pytest.fixture
def other():
    return Vendor.objects.create(name="Other Corp")


def _system(vendor, name, owner=None):
    return System.objects.create(vendor=vendor, name=name, owner_vendor=owner,
                                 published=True)


def _component(vendor, name, owner=None):
    return Component.objects.create(vendor=vendor, name=name, owner_vendor=owner,
                                    kind=ComponentKind.gpu.value, published=True)


# --- the enumeration is the contract -----------------------------------------


def test_the_enumeration_covers_every_model_that_qualifies():
    """The point of the constant is that nothing is left out of it.

    Any concrete model with both a ``vendor`` and an ``owner_vendor`` FK must be
    listed, or a merge leaves it pointing at a deleted vendor. The equality also
    subsumes the reverse direction: an entry that lacked either FK could not appear
    in ``qualifying``, so a typo in the constant fails here too.
    """
    qualifying = set()
    for model in apps.get_models():
        names = {f.name for f in model._meta.get_fields()}
        if {"vendor", "owner_vendor"} <= names:
            qualifying.add((model._meta.app_label, model.__name__))

    assert qualifying == set(OWNED_LISTING_MODELS)


# --- transfer ----------------------------------------------------------------


def test_ownerless_listings_transfer_to_the_claiming_vendor(acme):
    _system(acme, "R750")
    _component(acme, "Acme GPU")

    moved = transfer_unowned_listings(acme)

    assert moved["systems"] == 1 and moved["components"] == 1
    assert System.objects.get(name="R750").owner_vendor_id == acme.pk
    assert Component.objects.get(name="Acme GPU").owner_vendor_id == acme.pk


def test_a_listing_owned_by_someone_else_is_left_alone(acme, other):
    """Claiming Acme must not take a listing Other already maintains, even if
    Acme is named as its manufacturer."""
    _system(acme, "Co-branded", owner=other)

    moved = transfer_unowned_listings(acme)

    assert not any(moved.values())
    assert System.objects.get(name="Co-branded").owner_vendor_id == other.pk


def test_listings_of_a_different_vendor_are_untouched(acme, other):
    _system(other, "Not Ours")

    transfer_unowned_listings(acme)

    assert System.objects.get(name="Not Ours").owner_vendor_id is None


def test_transfer_is_idempotent(acme):
    _system(acme, "R750")

    transfer_unowned_listings(acme)
    second = transfer_unowned_listings(acme)

    assert not any(second.values())


# --- merge reads the same constant -------------------------------------------


def test_merging_repoints_both_vendor_and_owner_vendor(acme, other):
    """The messy real-world case: a community member inline-created a near
    duplicate, so the merge has to move manufacturer and maintainer alike."""
    _system(other, "Dup Manufacturer")
    _system(acme, "Dup Owner", owner=other)

    merge_vendors(acme, other)

    assert System.objects.get(name="Dup Manufacturer").vendor_id == acme.pk
    assert System.objects.get(name="Dup Owner").owner_vendor_id == acme.pk
    assert not Vendor.objects.filter(name="Other Corp").exists()


def test_merge_reports_counts_per_model(acme, other):
    _system(other, "S1")
    _component(other, "C1")

    moved = merge_vendors(acme, other)

    assert moved["systems"] == 1
    assert moved["components"] == 1
