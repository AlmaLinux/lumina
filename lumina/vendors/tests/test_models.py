"""Tests for Vendor and VendorMembership.

Behavior these tests pin down:

- ``Vendor`` auto-slugs from its name on save.
- ``Vendor.verified`` defaults to False; only admins set it to True, which
  is what gates ``vendor``-level trust for submissions made on its behalf.
- ``VendorMembership`` roles:
  - ``member`` cannot submit on behalf of the vendor.
  - ``submitter`` and ``owner`` can.
- A user cannot have two memberships for the same vendor.
- ``user.vendors_for_submission()`` helper returns only the vendors where the
  user's role is in SUBMIT_ROLES - this is what the submit form's "on behalf
  of" dropdown will use.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError

from lumina.vendors.models import Vendor, VendorMembership
from lumina.vendors.services import vendors_for_submission

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture
def user():
    return User.objects.create_user(username="alice")


@pytest.fixture
def dell():
    return Vendor.objects.create(name="Dell EMC")


class VendorTests:
    def test_auto_slug_from_name(self):
        v = Vendor.objects.create(name="Super Micro Computer, Inc.")
        assert v.slug == "super-micro-computer-inc"

    def test_explicit_slug_preserved(self):
        v = Vendor.objects.create(name="HPE", slug="hewlett-packard-enterprise")
        assert v.slug == "hewlett-packard-enterprise"

    def test_name_is_unique(self, dell):
        with pytest.raises(IntegrityError):
            Vendor.objects.create(name="Dell EMC")


class VendorMembershipTests:
    def test_default_member_role_cannot_submit(self, user, dell):
        m = VendorMembership.objects.create(user=user, vendor=dell)
        assert m.can_submit is False

    def test_submitter_can_submit(self, user, dell):
        m = VendorMembership.objects.create(
            user=user, vendor=dell, role=VendorMembership.ROLE_SUBMITTER
        )
        assert m.can_submit is True

    def test_owner_can_submit(self, user, dell):
        m = VendorMembership.objects.create(
            user=user, vendor=dell, role=VendorMembership.ROLE_OWNER
        )
        assert m.can_submit is True

    def test_unique_per_user_and_vendor(self, user, dell):
        VendorMembership.objects.create(user=user, vendor=dell)
        with pytest.raises(IntegrityError):
            VendorMembership.objects.create(user=user, vendor=dell)


class VendorsForSubmissionTests:
    def test_returns_only_submitter_or_owner_vendors(self, user, dell):
        supermicro = Vendor.objects.create(name="Supermicro")
        hpe = Vendor.objects.create(name="HPE")
        VendorMembership.objects.create(user=user, vendor=dell, role=VendorMembership.ROLE_MEMBER)
        VendorMembership.objects.create(user=user, vendor=supermicro, role=VendorMembership.ROLE_SUBMITTER)
        VendorMembership.objects.create(user=user, vendor=hpe, role=VendorMembership.ROLE_OWNER)

        qs = vendors_for_submission(user)
        assert set(qs) == {supermicro, hpe}

    def test_anonymous_returns_empty(self, dell):
        from django.contrib.auth.models import AnonymousUser
        assert list(vendors_for_submission(AnonymousUser())) == []

    def test_user_with_no_memberships_returns_empty(self, user):
        assert list(vendors_for_submission(user)) == []
