"""Tests for the Vendor profile proposal workflow.

Two flavors share one VendorProposal table and state machine:

- ``create``: any authenticated user may propose a brand-new Vendor. Target
  is null until approval; on approval a Vendor is created from the
  proposed fields.
- ``update``: a user with a submit-role VendorMembership (submitter or owner)
  may propose an edit to their vendor's profile. Target is set to the
  existing Vendor; on approval the Vendor row is updated.

Reviewer actions (approve/reject/request_changes) mirror hardware submissions.
Approval is idempotent-guarded - you can't approve twice.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile

from lumina.vendors.models import Vendor, VendorMembership, VendorProposal
from lumina.vendors.services import (
    can_propose_vendor_edit,
    propose_new_vendor,
    propose_vendor_edit,
)

pytestmark = pytest.mark.django_db
User = get_user_model()


# 1x1 px PNG so ImageField validation doesn't trip on empty bytes.
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c636001000000050001"
    "0a2db40000000049454e44ae426082"
)


def _png(name: str = "logo.png") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, _PNG, content_type="image/png")


@pytest.fixture
def proposer():
    return User.objects.create_user(username="proposer")


@pytest.fixture
def reviewer():
    return User.objects.create_user(username="reviewer")


@pytest.fixture
def dell():
    return Vendor.objects.create(name="Dell")


class ProposeNewVendorTests:
    def test_any_authed_user_can_propose(self, proposer):
        proposal = propose_new_vendor(
            proposed_by=proposer,
            name="NewVendor Inc.",
            homepage="https://newvendor.example",
            description="Hardware co.",
            logo=_png(),
        )
        assert proposal.kind == VendorProposal.KIND_CREATE
        assert proposal.status == VendorProposal.STATUS_PENDING
        assert proposal.target is None
        assert proposal.proposed_by == proposer

    def test_duplicate_name_rejected_before_proposal_saved(self, proposer, dell):
        # Don't let two people propose a new vendor named "Dell" when Dell
        # already exists - that's just spam. Duplicate detection here keeps
        # the review queue clean; real de-duping on approval is also guarded.
        with pytest.raises(ValueError):
            propose_new_vendor(proposed_by=proposer, name="Dell")

    def test_approve_creates_vendor(self, proposer, reviewer):
        proposal = propose_new_vendor(
            proposed_by=proposer,
            name="NewCo",
            homepage="https://newco.example",
            description="",
            logo=_png(),
        )
        proposal.approve(by=reviewer)
        assert proposal.status == VendorProposal.STATUS_APPROVED
        v = Vendor.objects.get(name="NewCo")
        assert proposal.target == v
        assert v.homepage == "https://newco.example"
        assert v.logo.name  # file was copied over

    def test_cannot_approve_twice(self, proposer, reviewer):
        proposal = propose_new_vendor(proposed_by=proposer, name="NewCo")
        proposal.approve(by=reviewer)
        with pytest.raises(ValueError):
            proposal.approve(by=reviewer)

    def test_reject_does_not_create_vendor(self, proposer, reviewer):
        proposal = propose_new_vendor(proposed_by=proposer, name="NewCo")
        proposal.reject(by=reviewer, reason="not a real company")
        assert proposal.status == VendorProposal.STATUS_REJECTED
        assert not Vendor.objects.filter(name="NewCo").exists()


class ProposeVendorEditTests:
    def test_plain_user_cannot_propose_edit(self, proposer, dell):
        assert can_propose_vendor_edit(proposer, dell) is False

    def test_member_role_cannot_propose_edit(self, proposer, dell):
        VendorMembership.objects.create(
            user=proposer, vendor=dell, role=VendorMembership.ROLE_MEMBER
        )
        assert can_propose_vendor_edit(proposer, dell) is False

    def test_submitter_can_propose_edit(self, proposer, dell):
        VendorMembership.objects.create(
            user=proposer, vendor=dell, role=VendorMembership.ROLE_SUBMITTER
        )
        assert can_propose_vendor_edit(proposer, dell) is True

    def test_owner_can_propose_edit(self, proposer, dell):
        VendorMembership.objects.create(
            user=proposer, vendor=dell, role=VendorMembership.ROLE_OWNER
        )
        assert can_propose_vendor_edit(proposer, dell) is True

    def test_service_checks_permission(self, proposer, dell):
        with pytest.raises(PermissionError):
            propose_vendor_edit(proposed_by=proposer, vendor=dell, homepage="https://new.example")

    def test_approve_updates_vendor(self, proposer, reviewer, dell):
        VendorMembership.objects.create(
            user=proposer, vendor=dell, role=VendorMembership.ROLE_SUBMITTER
        )
        proposal = propose_vendor_edit(
            proposed_by=proposer, vendor=dell,
            homepage="https://dell.example/new",
            description="Updated description.",
        )
        proposal.approve(by=reviewer)

        dell.refresh_from_db()
        assert dell.homepage == "https://dell.example/new"
        assert dell.description == "Updated description."

    def test_edit_preserves_existing_logo_when_no_new_logo(self, proposer, reviewer, dell):
        VendorMembership.objects.create(
            user=proposer, vendor=dell, role=VendorMembership.ROLE_SUBMITTER
        )
        dell.logo = _png("original.png")
        dell.save()
        original_path = dell.logo.name

        proposal = propose_vendor_edit(
            proposed_by=proposer, vendor=dell,
            homepage="https://dell.example",
            logo=None,  # no new logo
        )
        proposal.approve(by=reviewer)
        dell.refresh_from_db()
        assert dell.logo.name == original_path

    def test_edit_replaces_logo_when_new_logo_provided(self, proposer, reviewer, dell):
        VendorMembership.objects.create(
            user=proposer, vendor=dell, role=VendorMembership.ROLE_SUBMITTER
        )
        proposal = propose_vendor_edit(
            proposed_by=proposer, vendor=dell,
            homepage="https://dell.example",
            logo=_png("new-logo.png"),
        )
        proposal.approve(by=reviewer)
        dell.refresh_from_db()
        assert dell.logo.name.endswith("new-logo.png")



def test_edit_with_a_new_logo_through_the_view(client, proposer, dell):
    """The vendor edit view accepts a new logo and files an update proposal.

    Covers the view/form path - ``VendorEditProposalForm.save(commit=False)`` plus the ImageField
    save - which the service-path tests never exercised. Uses a real, valid PNG so the form passes
    and the save actually runs.
    """
    import io

    from django.urls import reverse
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (4, 4), "blue").save(buf, format="PNG")
    VendorMembership.objects.create(
        user=proposer, vendor=dell, role=VendorMembership.ROLE_SUBMITTER
    )
    client.force_login(proposer)
    resp = client.post(
        reverse("vendors:propose_edit", args=[dell.slug]),
        {
            "name": dell.name, "homepage": "https://dell.example", "contact_email": "",
            "description": "A new description.",
            "logo": SimpleUploadedFile("logo.png", buf.getvalue(), content_type="image/png"),
        },
    )
    assert resp.status_code == 302, resp.content[:600]
    assert VendorProposal.objects.filter(target=dell, kind=VendorProposal.KIND_UPDATE).exists()


# --- propose a new vendor, optionally claiming ownership -------------------------


def _new_vendor_url():
    from django.urls import reverse
    return reverse("vendors:propose_new")


class ProposeNewWithClaimTests:
    def test_without_a_claim_it_files_a_plain_proposal(self, client, proposer):
        client.force_login(proposer)
        resp = client.post(_new_vendor_url(), {
            "name": "Orbital Forge", "scope": Vendor.SCOPE_SOFTWARE,
            "homepage": "", "contact_email": "", "description": "",
        })

        assert resp.status_code == 302, resp.content[:600]
        proposal = VendorProposal.objects.get()
        assert proposal.kind == VendorProposal.KIND_CREATE
        assert proposal.scope == Vendor.SCOPE_SOFTWARE
        # Nothing exists until a reviewer approves the proposal.
        assert not Vendor.objects.filter(name="Orbital Forge").exists()

    def test_with_a_claim_it_creates_an_unpublished_vendor_and_a_claim(self, client, proposer):
        from lumina.vendors.models import VendorClaim

        client.force_login(proposer)
        resp = client.post(_new_vendor_url(), {
            "name": "Orbital Forge", "scope": Vendor.SCOPE_SOFTWARE,
            "homepage": "", "contact_email": "", "description": "",
            "claim_ownership": "on",
            "work_email": "sam@orbitalforge.example", "role_at_vendor": "Founder", "note": "",
        })

        assert resp.status_code == 302, resp.content[:600]
        # A claim on a fresh, unpublished, unclaimed vendor - not a plain proposal.
        assert not VendorProposal.objects.exists()
        vendor = Vendor.objects.get(name="Orbital Forge")
        assert vendor.published is False
        assert vendor.scope == Vendor.SCOPE_SOFTWARE
        assert vendor.is_claimed is False
        claim = VendorClaim.objects.get(vendor=vendor, requester=proposer)
        assert claim.work_email == "sam@orbitalforge.example"
        assert claim.role_at_vendor == "Founder"

    def test_ticking_the_claim_requires_the_verification_inputs(self, client, proposer):
        client.force_login(proposer)
        resp = client.post(_new_vendor_url(), {
            "name": "Orbital Forge", "scope": Vendor.SCOPE_SOFTWARE,
            "claim_ownership": "on",  # ticked, but no work_email / role
        })

        assert resp.status_code == 200, "re-renders with errors rather than creating anything"
        assert not Vendor.objects.filter(name="Orbital Forge").exists()

    def test_approving_the_claim_publishes_owns_and_verifies(self, client, proposer, reviewer):
        from lumina.vendors.models import VendorClaim, VendorMembership

        client.force_login(proposer)
        client.post(_new_vendor_url(), {
            "name": "Orbital Forge", "scope": Vendor.SCOPE_SOFTWARE,
            "claim_ownership": "on",
            "work_email": "sam@orbitalforge.example", "role_at_vendor": "Founder",
        })
        claim = VendorClaim.objects.get()

        claim.approve(by=reviewer, verify=True)
        vendor = claim.vendor
        vendor.refresh_from_db()

        assert vendor.published is True
        assert vendor.verified is True
        assert vendor.is_claimed is True
        assert VendorMembership.objects.get(
            user=proposer, vendor=vendor
        ).role == VendorMembership.ROLE_OWNER

    def test_the_service_reuses_creation_and_claim(self, proposer):
        """``submit_vendor_with_claim`` is the two existing pieces, not a third path."""
        from lumina.vendors.models import VendorClaim
        from lumina.vendors.services import submit_vendor_with_claim

        claim = submit_vendor_with_claim(
            name="Orbital Forge", scope=Vendor.SCOPE_HARDWARE,
            requester=proposer, work_email="sam@orbitalforge.example",
            role_at_vendor="Founder",
        )

        assert isinstance(claim, VendorClaim)
        assert claim.vendor.published is False
        assert claim.vendor.is_claimed is False
