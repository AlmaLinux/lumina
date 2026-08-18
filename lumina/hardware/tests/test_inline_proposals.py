"""Tests for inline vendor + CPU proposals on the submission form.

When a submitter doesn't see their vendor in the dropdown, or wants to
declare a CPU family the catalog doesn't yet know about, they can fill in
the inline section right inside the submit form - no separate proposal
flow. The inline-created entities land in draft state (published=False)
and are cascade-published when the submission itself is approved.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from lumina.core.certification import ValidationLevel
from lumina.hardware.models import Component, ComponentKind, System
from lumina.vendors.models import Vendor, VendorMembership

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture
def alice():
    return User.objects.create_user(username="alice")


@pytest.fixture
def reviewer():
    return User.objects.create_user(username="rev")


@pytest.fixture
def existing_vendor():
    return Vendor.objects.create(name="Existing", published=True)


# --- Vendor lifecycle --------------------------------------------------- #

class VendorPublishedFlagTests:
    def test_default_is_published(self):
        v = Vendor.objects.create(name="X")
        assert v.published is True

    def test_unpublished_excluded_from_published_queryset(self):
        Vendor.objects.create(name="published", published=True)
        Vendor.objects.create(name="hidden", published=False)
        names = list(Vendor.objects.published().values_list("name", flat=True))
        assert "published" in names and "hidden" not in names


# --- Inline new vendor on submit form ----------------------------------- #

class InlineNewVendorOnSubmitTests:
    def test_picks_existing_vendor_normally(self, alice, existing_vendor, client):
        client.force_login(alice)
        resp = client.post("/submit/", {
            "kind": "system",
            "name": "Box A",
            "model_number": "A1",
            "vendor": existing_vendor.slug,
            "claimed_validation_level": ValidationLevel.COMMUNITY,
        })
        assert resp.status_code == 302
        listing = System.objects.get(name="Box A")
        assert listing.vendor == existing_vendor

    def test_inline_new_vendor_creates_draft_vendor(self, alice, client):
        client.force_login(alice)
        resp = client.post("/submit/", {
            "kind": "system",
            "name": "Box B",
            "model_number": "B1",
            "vendor": "__new__",
            "new_vendor_name": "Brand New Co",
            "new_vendor_homepage": "https://newco.example",
            "claimed_validation_level": ValidationLevel.COMMUNITY,
        })
        assert resp.status_code == 302
        v = Vendor.objects.get(name="Brand New Co")
        assert v.published is False
        listing = System.objects.get(name="Box B")
        assert listing.vendor == v

    def test_inline_vendor_grants_submit_rights_not_ownership(self, alice, client):
        """This test used to assert ``ROLE_OWNER``, and that was the squatting bug.

        Typing a vendor's name into a submit form is not evidence of representing
        them, but ownership is what ``Vendor.is_claimed`` reads and what
        ``derive_allowed_levels`` turns into the vendor tier once the vendor is
        verified. A stranger who typed "Dell" therefore held Dell's identity and
        could have submitted Vendor-validated content in their name.

        ``ROLE_SUBMITTER`` keeps everything they actually need - submitting for and
        editing their own listing, since ``SUBMIT_ROLES`` covers both - while
        leaving the identity vacant for the real vendor to claim.
        """
        client.force_login(alice)
        client.post("/submit/", {
            "kind": "system",
            "name": "Box C",
            "model_number": "C1",
            "vendor": "__new__",
            "new_vendor_name": "Propose Me",
            "claimed_validation_level": ValidationLevel.COMMUNITY,
        })
        v = Vendor.objects.get(name="Propose Me")

        assert VendorMembership.objects.filter(
            user=alice, vendor=v, role=VendorMembership.ROLE_SUBMITTER,
        ).exists()
        assert not VendorMembership.objects.filter(
            vendor=v, role=VendorMembership.ROLE_OWNER,
        ).exists()
        assert v.is_claimed is False

    def test_inline_vendor_name_required_when_chosen(self, alice, client):
        client.force_login(alice)
        resp = client.post("/submit/", {
            "kind": "system",
            "name": "Box D",
            "model_number": "D1",
            "vendor": "__new__",
            "new_vendor_name": "",  # missing
            "claimed_validation_level": ValidationLevel.COMMUNITY,
        })
        # Form should re-render with errors, not 302-redirect.
        assert resp.status_code == 200
        assert not Vendor.objects.filter(name="").exists()
        assert not System.objects.filter(name="Box D").exists()


# --- Inline new CPU family on submit form ------------------------------- #

class InlineNewCpuOnSubmitTests:
    def test_inline_cpu_creates_draft_component(self, alice, existing_vendor, client):
        intel = Vendor.objects.get_or_create(name="Intel", defaults={"published": True})[0]
        client.force_login(alice)
        resp = client.post("/submit/", {
            "kind": "system",
            "name": "Box E",
            "model_number": "E1",
            "vendor": existing_vendor.slug,
            "claimed_validation_level": ValidationLevel.COMMUNITY,
            "new_cpu_name_0": "Made-Up CPU Family",
            "new_cpu_vendor_0": intel.slug,
        })
        assert resp.status_code == 302
        cpu = Component.objects.get(name="Made-Up CPU Family")
        assert cpu.kind == ComponentKind.cpu.value
        assert cpu.published is False
        assert cpu.vendor == intel
        # Auto-attached to the new system's CPU set.
        listing = System.objects.get(name="Box E")
        assert cpu in listing.cpus.all()

    def test_inline_cpu_blank_row_ignored(self, alice, existing_vendor, client):
        client.force_login(alice)
        client.post("/submit/", {
            "kind": "system",
            "name": "Box F",
            "model_number": "F1",
            "vendor": existing_vendor.slug,
            "claimed_validation_level": ValidationLevel.COMMUNITY,
            "new_cpu_name_0": "",
            "new_cpu_vendor_0": existing_vendor.slug,
        })
        # No Component should be created from an empty name row.
        assert not Component.objects.filter(vendor=existing_vendor, kind=ComponentKind.cpu.value).exists()


# --- Approval cascades --------------------------------------------------- #

class ApprovalCascadesTests:
    def test_approve_publishes_inline_vendor_and_cpus(self, alice, reviewer, client):
        client.force_login(alice)
        intel = Vendor.objects.get_or_create(name="Intel", defaults={"published": True})[0]
        client.post("/submit/", {
            "kind": "system",
            "name": "Box G",
            "model_number": "G1",
            "vendor": "__new__",
            "new_vendor_name": "Cascade Co",
            "claimed_validation_level": ValidationLevel.COMMUNITY,
            "new_cpu_name_0": "Cascade CPU",
            "new_cpu_vendor_0": intel.slug,
        })
        listing = System.objects.get(name="Box G")
        new_vendor = Vendor.objects.get(name="Cascade Co")
        new_cpu = Component.objects.get(name="Cascade CPU")
        assert new_vendor.published is False
        assert new_cpu.published is False

        from lumina.hardware.models import Submission
        sub = Submission.objects.get(listing_system=listing)
        sub.approve(by=reviewer, final_level=ValidationLevel.COMMUNITY)

        new_vendor.refresh_from_db()
        new_cpu.refresh_from_db()
        listing.refresh_from_db()
        assert new_vendor.published is True
        assert new_cpu.published is True
        assert listing.published is True
