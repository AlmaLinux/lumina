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
