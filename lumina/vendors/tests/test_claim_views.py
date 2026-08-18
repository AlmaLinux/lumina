"""The claim page and the reviewer endpoints that act on a claim."""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.urls import reverse

from lumina.hardware.models import System
from lumina.vendors.models import VendorClaim, VendorMembership
from lumina.vendors.services import create_inline_vendor, is_claimable

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture
def acme(db):
    squatter = User.objects.create_user("squatter", email="sq@example.com")
    return create_inline_vendor(name="Acme", created_by=squatter)


@pytest.fixture
def engineer(client):
    user = User.objects.create_user("alice", email="alice@acme.com")
    client.force_login(user)
    return user


@pytest.fixture
def reviewer(client):
    user = User.objects.create_user("rev", email="rev@example.com")
    user.groups.add(Group.objects.get_or_create(name="reviewer")[0])
    client.force_login(user)
    return user


PAYLOAD = {
    "work_email": "alice@acme.com",
    "role_at_vendor": "Release Engineering",
    "note": "I maintain the RPM builds.",
}


def test_the_claim_page_requires_login(client, acme):
    resp = client.get(reverse("vendors:claim", args=[acme.slug]))
    assert resp.status_code in (302, 403)


def test_submitting_a_claim_creates_a_pending_row(client, acme, engineer):
    resp = client.post(reverse("vendors:claim", args=[acme.slug]), PAYLOAD, follow=True)

    assert resp.status_code == 200
    claim = VendorClaim.objects.get(vendor=acme, requester=engineer)
    assert claim.status == VendorClaim.STATUS_PENDING


def test_a_second_open_claim_is_reported_not_crashed(client, acme, engineer):
    client.post(reverse("vendors:claim", args=[acme.slug]), PAYLOAD)

    resp = client.post(reverse("vendors:claim", args=[acme.slug]), PAYLOAD, follow=True)

    assert resp.status_code == 200
    assert VendorClaim.objects.filter(vendor=acme, requester=engineer).count() == 1


def test_a_pending_claim_shows_in_the_review_queue(client, acme, engineer, reviewer):
    from lumina.vendors.services import claim_vendor
    claim_vendor(vendor=acme, requester=engineer, **PAYLOAD)

    body = client.get(reverse("review:queue")).content.decode()

    assert "Acme" in body
    assert "claim" in body.lower()


def test_approving_from_the_review_page_transfers_ownership(client, acme, engineer,
                                                            reviewer):
    from lumina.vendors.services import claim_vendor
    System.objects.create(vendor=acme, name="Acme Box", published=True)
    claim = claim_vendor(vendor=acme, requester=engineer, **PAYLOAD)

    client.post(reverse("review:vendor_claim_approve", args=[claim.pk]),
                {"verify": "on"}, follow=True)

    claim.refresh_from_db()
    acme.refresh_from_db()
    assert claim.status == VendorClaim.STATUS_APPROVED
    assert acme.verified is True
    assert System.objects.get(name="Acme Box").owner_vendor_id == acme.pk
    assert VendorMembership.objects.get(
        user=engineer, vendor=acme
    ).role == VendorMembership.ROLE_OWNER


def test_approving_without_ticking_verify_leaves_the_vendor_unverified(
    client, acme, engineer, reviewer
):
    from lumina.vendors.services import claim_vendor
    claim = claim_vendor(vendor=acme, requester=engineer, **PAYLOAD)

    client.post(reverse("review:vendor_claim_approve", args=[claim.pk]), {}, follow=True)

    acme.refresh_from_db()
    assert acme.verified is False
    assert acme.is_claimed is True


def test_a_double_approve_is_a_message_not_a_500(client, acme, engineer, reviewer):
    """Hardware's submission endpoints return a 500 here; the claim endpoints
    must not repeat that."""
    from lumina.vendors.services import claim_vendor
    claim = claim_vendor(vendor=acme, requester=engineer, **PAYLOAD)
    url = reverse("review:vendor_claim_approve", args=[claim.pk])
    client.post(url, {"verify": "on"})

    resp = client.post(url, {"verify": "on"}, follow=True)

    assert resp.status_code == 200


def test_rejecting_a_claim_records_the_reason(client, acme, engineer, reviewer):
    from lumina.vendors.services import claim_vendor
    claim = claim_vendor(vendor=acme, requester=engineer, **PAYLOAD)

    client.post(reverse("review:vendor_claim_reject", args=[claim.pk]),
                {"reason": "Need proof from an acme.com address."}, follow=True)

    claim.refresh_from_db()
    assert claim.status == VendorClaim.STATUS_REJECTED
    assert "acme.com" in claim.reviewer_notes
    assert acme.is_claimed is False


def test_a_non_reviewer_cannot_approve(client, acme, engineer):
    from lumina.vendors.services import claim_vendor
    claim = claim_vendor(vendor=acme, requester=engineer, **PAYLOAD)

    resp = client.post(reverse("review:vendor_claim_approve", args=[claim.pk]),
                       {"verify": "on"})

    assert resp.status_code == 403
    claim.refresh_from_db()
    assert claim.status == VendorClaim.STATUS_PENDING


# --- the entry point on a listing ---------------------------------------------


def test_a_listing_offers_the_claim_link_while_the_vendor_is_unclaimed(client, acme):
    """lumina has no public vendor pages, so a listing is the only doorway."""
    from lumina.hardware.models import System

    listing = System.objects.create(vendor=acme, name="Acme Box", published=True)
    acme.published = True
    acme.save(update_fields=["published"])

    body = client.get(reverse("hardware:detail", args=[listing.slug])).content.decode()

    assert "Are you the vendor?" in body
    assert reverse("vendors:claim", args=[acme.slug]) in body


def test_the_claim_link_disappears_once_the_vendor_is_claimed(client, acme,
):
    from lumina.hardware.models import System

    owner = User.objects.create_user("owner", email="o@example.com")
    VendorMembership.objects.create(
        user=owner, vendor=acme, role=VendorMembership.ROLE_OWNER,
    )
    listing = System.objects.create(vendor=acme, name="Acme Box", published=True)
    acme.published = True
    acme.save(update_fields=["published"])

    body = client.get(reverse("hardware:detail", args=[listing.slug])).content.decode()

    assert "Are you the vendor?" not in body


def test_a_verified_vendor_is_not_advertised_as_claimable(client, acme):
    """Verified is the one state we do not solicit claims on.

    ``derive_allowed_levels`` grants the vendor tier to any submit-role member of
    a *verified* vendor, so approving a claim here unlocks vendor-validated
    submissions on the spot - there is no separate verify decision left for the
    reviewer to weigh, the way there is on an unverified vendor. That makes a
    verified vendor the most valuable thing in the system to impersonate, so its
    listings stop inviting the attempt.
    """
    acme.verified = True
    acme.published = True
    acme.save(update_fields=["verified", "published"])
    listing = System.objects.create(vendor=acme, name="Acme Box", published=True)

    body = client.get(reverse("hardware:detail", args=[listing.slug])).content.decode()

    assert "Are you the vendor?" not in body


def test_the_claim_page_stays_reachable_for_a_verified_vendor(client, acme, engineer):
    """Unadvertised, not blocked.

    A verified vendor with no owner is a real state: the SIG can vouch for a
    company before anyone from it has an account. Hiding the button removes the
    open invitation while leaving a reviewer able to send the real contact a
    working link.
    """
    acme.verified = True
    acme.save(update_fields=["verified"])

    response = client.get(reverse("vendors:claim", args=[acme.slug]))

    assert response.status_code == 200


def test_is_claimable_needs_both_unowned_and_unverified(acme):
    assert is_claimable(acme) is True

    acme.verified = True
    assert is_claimable(acme) is False

    acme.verified = False
    VendorMembership.objects.create(
        user=User.objects.create_user("owner2", email="o2@example.com"),
        vendor=acme,
        role=VendorMembership.ROLE_OWNER,
    )
    assert is_claimable(acme) is False
