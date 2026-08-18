"""Vendor scope, and who owns a vendor a stranger typed into a submit form.

- ``Vendor.scope`` separates the hardware and software vendor directories while
  memberships, aliases, and verification stay shared.
- ``VendorProposal`` carries the scope through approval, or a software vendor
  proposed through the public form is created as a hardware vendor and never
  appears in a software picker.
- Inline vendor creation must NOT hand the vendor identity to whoever typed the
  name. It previously granted ROLE_OWNER, so a community member submitting
  "Acme Backup" became owner of the Acme vendor record, and the real Acme had
  nothing left to claim.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from lumina.vendors.models import Vendor, VendorMembership, VendorProposal

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture
def stranger():
    return User.objects.create_user("stranger", email="s@example.com")


# --- scope --------------------------------------------------------------------


def test_existing_vendors_are_hardware_scoped_by_default():
    """Every vendor in the catalog today is a hardware vendor, so the field's
    default is what backfills them; no data migration is needed."""
    vendor = Vendor.objects.create(name="Legacy Co")
    assert vendor.scope == Vendor.SCOPE_HARDWARE


def test_each_directory_sees_only_its_own_vendors_plus_the_shared_ones():
    Vendor.objects.create(name="HW Only", scope=Vendor.SCOPE_HARDWARE)
    Vendor.objects.create(name="SW Only", scope=Vendor.SCOPE_SOFTWARE)
    Vendor.objects.create(name="Both Co", scope=Vendor.SCOPE_BOTH)

    # Scoped to the three created here: data migrations seed real GPU/CPU
    # vendors, which are hardware-scoped and would otherwise pad the sets.
    mine = ["HW Only", "SW Only", "Both Co"]
    hardware = set(Vendor.objects.for_scope(Vendor.SCOPE_HARDWARE)
                   .filter(name__in=mine).values_list("name", flat=True))
    software = set(Vendor.objects.for_scope(Vendor.SCOPE_SOFTWARE)
                   .filter(name__in=mine).values_list("name", flat=True))

    assert hardware == {"HW Only", "Both Co"}
    assert software == {"SW Only", "Both Co"}


def test_a_proposal_carries_its_scope_onto_the_vendor_it_creates(stranger):
    """Without this a software vendor proposed at /vendors/propose-new/ is
    created as hardware and is invisible in every software picker."""
    reviewer = User.objects.create_user("rev", email="r@example.com")
    proposal = VendorProposal.objects.create(
        kind=VendorProposal.KIND_CREATE,
        proposed_by=stranger,
        name="Acme Software",
        scope=Vendor.SCOPE_SOFTWARE,
    )

    proposal.approve(by=reviewer)

    assert Vendor.objects.get(name="Acme Software").scope == Vendor.SCOPE_SOFTWARE


# --- squatting ----------------------------------------------------------------


def test_inline_vendor_creation_grants_the_creator_nothing():
    """The whole point of the claim flow is that the identity stays available. Naming a vendor
    inline enrolls nobody, so membership of any role - and ownership - is left for a claim."""
    from lumina.vendors.services import create_inline_vendor

    vendor = create_inline_vendor(name="Acme", scope=Vendor.SCOPE_SOFTWARE)

    assert not VendorMembership.objects.filter(vendor=vendor).exists()
    assert not vendor.is_claimed


def test_an_inline_vendor_starts_unpublished_and_unclaimed():
    from lumina.vendors.services import create_inline_vendor

    vendor = create_inline_vendor(name="Acme")

    assert vendor.published is False
    assert vendor.is_claimed is False


def test_a_vendor_with_an_owner_reads_as_claimed(stranger):
    vendor = Vendor.objects.create(name="Acme")
    VendorMembership.objects.create(
        user=stranger, vendor=vendor, role=VendorMembership.ROLE_OWNER,
    )

    assert vendor.is_claimed is True


def test_a_submitter_role_alone_does_not_make_a_vendor_claimed(stranger):
    """Submit rights are not identity: this is what leaves the door open for
    the real vendor to claim it later."""
    vendor = Vendor.objects.create(name="Acme")
    VendorMembership.objects.create(
        user=stranger, vendor=vendor, role=VendorMembership.ROLE_SUBMITTER,
    )

    assert vendor.is_claimed is False
