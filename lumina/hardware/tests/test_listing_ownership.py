"""Tests for listing ownership.

A vendor "owns" a listing when ``owner_vendor`` points at it. Ownership is
how we decide who may submit modification proposals to a published listing
moving forward:

- Submitted on behalf of a vendor → owner_vendor = that vendor, automatically.
- Submitted as a plain user (no vendor backing) → owner_vendor stays null.
  No one can ever propose edits via the self-service flow; only an admin
  can change such a listing or assign ownership later.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from lumina.core.certification import ValidationLevel
from lumina.hardware.forms import SubmissionForm
from lumina.hardware.models import System
from lumina.vendors.models import Vendor, VendorMembership
from lumina.vendors.services import can_edit_listing

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture
def alice():
    return User.objects.create_user(username="alice")


@pytest.fixture
def vendor_user():
    u = User.objects.create_user(username="dell-rep")
    return u


@pytest.fixture
def dell():
    return Vendor.objects.create(name="Dell", verified=True)


@pytest.fixture
def hpe():
    return Vendor.objects.create(name="HPE", verified=True)


def _payload(vendor: Vendor, **overrides) -> dict:
    data = {
        "kind": "system",
        "name": "PowerEdge R750",
        "model_number": "R750",
        "vendor": vendor.slug,
        "claimed_validation_level": ValidationLevel.COMMUNITY,
    }
    data.update(overrides)
    return data


class OwnerAssignmentOnSubmitTests:
    def test_plain_user_submission_has_no_owner(self, alice, dell):
        form = SubmissionForm(data=_payload(dell), user=alice)
        assert form.is_valid(), form.errors
        sub = form.save()
        assert sub.listing.owner_vendor is None

    def test_on_behalf_submission_assigns_owner(self, vendor_user, dell):
        VendorMembership.objects.create(
            user=vendor_user, vendor=dell, role=VendorMembership.ROLE_OWNER,
        )
        form = SubmissionForm(
            data=_payload(
                dell, on_behalf_of=dell.slug,
                claimed_validation_level=ValidationLevel.VENDOR,
            ),
            user=vendor_user,
        )
        assert form.is_valid(), form.errors
        sub = form.save()
        assert sub.listing.owner_vendor == dell


class CanEditListingTests:
    def test_owner_with_submit_role_can_edit(self, vendor_user, dell):
        VendorMembership.objects.create(
            user=vendor_user, vendor=dell, role=VendorMembership.ROLE_SUBMITTER,
        )
        listing = System.objects.create(
            name="x", vendor=dell, model_number="x", owner_vendor=dell,
        )
        assert can_edit_listing(vendor_user, listing) is True

    def test_member_role_cannot_edit(self, vendor_user, dell):
        VendorMembership.objects.create(
            user=vendor_user, vendor=dell, role=VendorMembership.ROLE_MEMBER,
        )
        listing = System.objects.create(
            name="x", vendor=dell, model_number="x", owner_vendor=dell,
        )
        assert can_edit_listing(vendor_user, listing) is False

    def test_unowned_listing_cannot_be_edited_by_anyone(self, vendor_user, dell):
        VendorMembership.objects.create(
            user=vendor_user, vendor=dell, role=VendorMembership.ROLE_OWNER,
        )
        listing = System.objects.create(
            name="x", vendor=dell, model_number="x", owner_vendor=None,
        )
        # Even though vendor_user is a Dell owner, the listing has no owner
        # so no one (other than admins via the Django admin) can edit it.
        assert can_edit_listing(vendor_user, listing) is False

    def test_member_of_different_vendor_cannot_edit(self, vendor_user, dell, hpe):
        VendorMembership.objects.create(
            user=vendor_user, vendor=hpe, role=VendorMembership.ROLE_OWNER,
        )
        listing = System.objects.create(
            name="x", vendor=dell, model_number="x", owner_vendor=dell,
        )
        assert can_edit_listing(vendor_user, listing) is False

    def test_anonymous_cannot_edit(self, dell):
        from django.contrib.auth.models import AnonymousUser
        listing = System.objects.create(
            name="x", vendor=dell, model_number="x", owner_vendor=dell,
        )
        assert can_edit_listing(AnonymousUser(), listing) is False
