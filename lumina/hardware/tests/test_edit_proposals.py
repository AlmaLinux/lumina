"""Tests for the listing edit-proposal flow.

A ListingEditProposal is the self-service path for vendor-owned listings:
submit-role members of the listing's owner vendor propose changes; the
proposal lands in the review queue; on approval the listing is updated.

Plain-user-owned-by-nobody listings have no edit path here - admins must
use the Django admin.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from lumina.core.certification import ValidationLevel
from lumina.hardware.models import (
    ListingEditProposal,
    System,
)
from lumina.hardware.services import propose_listing_edit
from lumina.vendors.models import Vendor, VendorMembership

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture
def proposer():
    return User.objects.create_user(username="dell-rep")


@pytest.fixture
def reviewer():
    return User.objects.create_user(username="rev")


@pytest.fixture
def dell():
    return Vendor.objects.create(name="Dell")


@pytest.fixture
def hpe():
    return Vendor.objects.create(name="HPE")


@pytest.fixture
def owned_listing(dell):
    return System.objects.create(
        name="PowerEdge R750",
        vendor=dell,
        owner_vendor=dell,
        model_number="R750",
        description="Original description.",
        vendor_spec_url="https://dell.example/specs/r750",
        published=True,
        validation_level=ValidationLevel.VENDOR,
    )


@pytest.fixture
def unowned_listing(dell):
    return System.objects.create(
        name="Community R750", vendor=dell, owner_vendor=None,
        model_number="R750-C", published=True,
    )


def test_cancel_on_a_draft_listing_does_not_point_at_a_404(
    client, proposer, dell
):
    """``_get_listing_for_edit`` does not filter on ``published``, so this form is
    reachable for a draft - but ``hardware:detail`` refuses one, so a plain Cancel
    link 404'd whoever had just decided not to change anything.
    """
    from django.urls import reverse

    VendorMembership.objects.create(
        user=proposer, vendor=dell, role=VendorMembership.ROLE_SUBMITTER,
    )
    draft = System.objects.create(
        name="Draft R750", vendor=dell, owner_vendor=dell, published=False,
    )
    client.force_login(proposer)

    body = client.get(
        reverse("hardware:propose_edit", args=[draft.slug])
    ).content.decode()

    assert reverse("hardware:detail", args=[draft.slug]) not in body
    assert reverse("accounts:dashboard") in body


class ProposeListingEditTests:
    def test_member_role_cannot_propose(self, proposer, dell, owned_listing):
        VendorMembership.objects.create(
            user=proposer, vendor=dell, role=VendorMembership.ROLE_MEMBER,
        )
        with pytest.raises(PermissionError):
            propose_listing_edit(proposed_by=proposer, listing=owned_listing)

    def test_submitter_can_propose(self, proposer, dell, owned_listing):
        VendorMembership.objects.create(
            user=proposer, vendor=dell, role=VendorMembership.ROLE_SUBMITTER,
        )
        proposal = propose_listing_edit(
            proposed_by=proposer, listing=owned_listing,
            description="Better description.",
        )
        assert proposal.status == ListingEditProposal.STATUS_PENDING
        assert proposal.target == owned_listing
        assert proposal.description == "Better description."

    def test_unowned_listing_rejects_proposal(self, proposer, dell, unowned_listing):
        VendorMembership.objects.create(
            user=proposer, vendor=dell, role=VendorMembership.ROLE_OWNER,
        )
        with pytest.raises(PermissionError):
            propose_listing_edit(proposed_by=proposer, listing=unowned_listing)

    def test_member_of_different_vendor_cannot_propose(self, proposer, hpe, owned_listing):
        VendorMembership.objects.create(
            user=proposer, vendor=hpe, role=VendorMembership.ROLE_OWNER,
        )
        with pytest.raises(PermissionError):
            propose_listing_edit(proposed_by=proposer, listing=owned_listing)


class ApproveEditProposalTests:
    def _approved_proposal(self, proposer, reviewer, dell, listing, **fields):
        VendorMembership.objects.create(
            user=proposer, vendor=dell, role=VendorMembership.ROLE_SUBMITTER,
        )
        proposal = propose_listing_edit(
            proposed_by=proposer, listing=listing, **fields
        )
        proposal.approve(by=reviewer)
        return proposal

    def test_approve_updates_provided_fields(self, proposer, reviewer, dell, owned_listing):
        self._approved_proposal(
            proposer, reviewer, dell, owned_listing,
            description="Updated description.",
            vendor_spec_url="https://dell.example/specs/r750-v2",
        )
        owned_listing.refresh_from_db()
        assert owned_listing.description == "Updated description."
        assert owned_listing.vendor_spec_url == "https://dell.example/specs/r750-v2"

    def test_blank_field_is_no_change(self, proposer, reviewer, dell, owned_listing):
        # Empty values on a proposal mean "don't touch the live field" -
        # a missing form field shouldn't blank out the listing.
        self._approved_proposal(
            proposer, reviewer, dell, owned_listing,
            description="Updated.",
            # vendor_spec_url omitted/blank - should keep its original value.
        )
        owned_listing.refresh_from_db()
        assert owned_listing.vendor_spec_url == "https://dell.example/specs/r750"

    def test_cannot_approve_twice(self, proposer, reviewer, dell, owned_listing):
        proposal = self._approved_proposal(
            proposer, reviewer, dell, owned_listing, description="x",
        )
        with pytest.raises(ValueError):
            proposal.approve(by=reviewer)


class RejectEditProposalTests:
    def test_reject_does_not_update_listing(self, proposer, reviewer, dell, owned_listing):
        VendorMembership.objects.create(
            user=proposer, vendor=dell, role=VendorMembership.ROLE_SUBMITTER,
        )
        proposal = propose_listing_edit(
            proposed_by=proposer, listing=owned_listing,
            description="Should not land.",
        )
        proposal.reject(by=reviewer, reason="too vague")
        owned_listing.refresh_from_db()
        assert owned_listing.description == "Original description."
        assert proposal.status == ListingEditProposal.STATUS_REJECTED
        assert proposal.reviewer_notes == "too vague"
