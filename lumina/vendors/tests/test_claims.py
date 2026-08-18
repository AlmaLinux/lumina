"""Claiming a vendor, which is how a community listing becomes vendor-validated.

The story this pins down: a community member creates listings naming Acme, and
later someone from Acme turns up wanting them. A claim is on the *vendor*, not
per listing, so one approval transfers everything already attributed to Acme -
across both catalogs.

Specifically:
- Approval grants owner membership, and only sets ``verified`` if the reviewer
  asked for it. Proving who you represent and being trusted to self-certify are
  separate decisions.
- Approval **demotes** other submit-role members. Otherwise the inline creator,
  who kept ROLE_SUBMITTER, would gain the vendor tier the moment the real vendor
  is verified, and could publish Vendor-validated content in Acme's name.
- One open claim per (vendor, requester), enforced in the service because a
  conditional UniqueConstraint is silently skipped on MariaDB (models.W036).
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from lumina.hardware.models import System
from lumina.vendors.models import VendorClaim, VendorMembership
from lumina.vendors.services import claim_vendor, create_inline_vendor

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture
def squatter():
    return User.objects.create_user("squatter", email="sq@example.com")


@pytest.fixture
def acme_engineer():
    return User.objects.create_user("alice", email="alice@acme.com")


@pytest.fixture
def reviewer():
    return User.objects.create_user("rev", email="rev@example.com")


@pytest.fixture
def inline_acme():
    """Acme as a stranger left it: unpublished, unverified, unclaimed - and now memberless, since
    inline creation enrolls nobody. Tests that need a prior member add one explicitly."""
    return create_inline_vendor(name="Acme")


def _claim(vendor, user):
    return claim_vendor(
        vendor=vendor, requester=user,
        work_email="alice@acme.com", role_at_vendor="Release Engineering",
        note="I maintain the RPMs.",
    )


# --- creating a claim ---------------------------------------------------------


def test_a_claim_starts_pending(inline_acme, acme_engineer):
    claim = _claim(inline_acme, acme_engineer)

    assert claim.status == VendorClaim.STATUS_PENDING
    assert claim.vendor == inline_acme


def test_a_second_open_claim_by_the_same_user_is_refused(inline_acme, acme_engineer):
    """The conditional unique constraint does not exist on MariaDB, so the
    service is the only thing actually enforcing this in production."""
    _claim(inline_acme, acme_engineer)

    with pytest.raises(ValueError, match="already"):
        _claim(inline_acme, acme_engineer)


def test_a_different_user_may_also_claim_the_same_vendor(inline_acme, acme_engineer,
                                                         squatter):
    """Two people can both assert they represent Acme; the reviewer decides."""
    _claim(inline_acme, acme_engineer)
    second = _claim(inline_acme, squatter)

    assert second.status == VendorClaim.STATUS_PENDING


def test_claiming_again_after_a_rejection_is_allowed(inline_acme, acme_engineer,
                                                     reviewer):
    """A rejection is not a permanent ban - the claimant may come back with
    better evidence."""
    first = _claim(inline_acme, acme_engineer)
    first.reject(by=reviewer, reason="Need proof from an acme.com address.")

    assert _claim(inline_acme, acme_engineer).status == VendorClaim.STATUS_PENDING


# --- approving ----------------------------------------------------------------


def test_approval_grants_ownership_and_publishes_the_vendor(inline_acme,
                                                            acme_engineer, reviewer):
    claim = _claim(inline_acme, acme_engineer)

    claim.approve(by=reviewer, verify=False)
    inline_acme.refresh_from_db()

    assert VendorMembership.objects.get(
        user=acme_engineer, vendor=inline_acme
    ).role == VendorMembership.ROLE_OWNER
    assert inline_acme.is_claimed
    assert inline_acme.published is True


def test_verification_is_a_separate_decision(inline_acme, acme_engineer, reviewer):
    """Proving you are from Acme does not by itself grant the vendor tier."""
    claim = _claim(inline_acme, acme_engineer)

    claim.approve(by=reviewer, verify=False)
    inline_acme.refresh_from_db()

    assert inline_acme.verified is False


def test_the_reviewer_can_verify_in_the_same_action(inline_acme, acme_engineer,
                                                    reviewer):
    claim = _claim(inline_acme, acme_engineer)

    claim.approve(by=reviewer, verify=True)
    inline_acme.refresh_from_db()

    assert inline_acme.verified is True


def test_approval_demotes_a_prior_submit_member(inline_acme, squatter,
                                                acme_engineer, reviewer):
    """Approving a claim demotes any existing submit-role member to plain member, so verifying the
    vendor does not hand the vendor tier to whoever was on the roster before the real rep claimed it.

    Inline creation no longer enrolls the person who typed the name (that hole is closed at the root
    - see ``test_scope_and_squatting``), so the prior member here is set up explicitly: the demotion
    still has to work for a member added by any other route.
    """
    from lumina.vendors.services import derive_allowed_levels

    VendorMembership.objects.create(
        user=squatter, vendor=inline_acme, role=VendorMembership.ROLE_SUBMITTER,
    )

    claim = _claim(inline_acme, acme_engineer)
    claim.approve(by=reviewer, verify=True)
    inline_acme.refresh_from_db()

    assert VendorMembership.objects.get(
        user=squatter, vendor=inline_acme
    ).role == VendorMembership.ROLE_MEMBER
    assert derive_allowed_levels(squatter, vendor=inline_acme) == ["community"]
    assert "vendor" in derive_allowed_levels(acme_engineer, vendor=inline_acme)


def test_demotion_can_be_declined_by_the_reviewer(inline_acme, squatter,
                                                  acme_engineer, reviewer):
    """Sometimes an existing member is a colleague, not a squatter, so the reviewer can keep them."""
    VendorMembership.objects.create(
        user=squatter, vendor=inline_acme, role=VendorMembership.ROLE_SUBMITTER,
    )
    claim = _claim(inline_acme, acme_engineer)

    claim.approve(by=reviewer, verify=True, demote_others=False)

    assert VendorMembership.objects.get(
        user=squatter, vendor=inline_acme
    ).role == VendorMembership.ROLE_SUBMITTER


def test_approval_transfers_the_vendors_unowned_listings(inline_acme, acme_engineer,
                                                          reviewer):
    """One approval, every listing - which is the whole reason the claim targets
    the vendor rather than each listing."""
    System.objects.create(vendor=inline_acme, name="Acme Box", published=True)
    claim = _claim(inline_acme, acme_engineer)

    claim.approve(by=reviewer, verify=True)

    assert System.objects.get(name="Acme Box").owner_vendor_id == inline_acme.pk


def test_approving_twice_is_refused(inline_acme, acme_engineer, reviewer):
    claim = _claim(inline_acme, acme_engineer)
    claim.approve(by=reviewer, verify=True)

    with pytest.raises(ValueError):
        claim.approve(by=reviewer, verify=True)
