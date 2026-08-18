"""The dashboard has to know about software, or a vendor's own page denies their
listings exist.

It queried System and Component only, so a software publisher saw an empty
workspace with no way back to their submissions or their open vendor claim.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from lumina.core.certification import ValidationLevel
from lumina.software.models import Software, SoftwareSubmission
from lumina.vendors.models import Vendor, VendorMembership

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture
def publisher(client):
    user = User.objects.create_user("pub", email="pub@example.com")
    client.force_login(user)
    return user


@pytest.fixture
def product(publisher):
    vendor = Vendor.objects.create(name="Vaultwise", scope=Vendor.SCOPE_SOFTWARE)
    VendorMembership.objects.create(
        user=publisher, vendor=vendor, role=VendorMembership.ROLE_SUBMITTER,
    )
    return Software.objects.create(
        vendor=vendor, name="Vaultwise Archive", published=True,
        owner_vendor=vendor, created_by=publisher,
    )


def test_the_quick_action_cards_offer_software_beside_hardware(
    client, publisher, product
):
    """The card row had a hardware submission card and no software one.

    Anchored on each card's own description sentence. The obvious assertions both
    pass without the card existing: ``software:submit`` is also the software
    table's empty-state link, and "Submit software" is already a sidebar nav item
    on every admin page.
    """
    body = client.get(reverse("accounts:dashboard")).content.decode()

    assert "Add a software product for review." in body
    assert "Add a new System or Component for review." in body


def test_the_dashboard_lists_the_users_software(client, publisher, product):
    body = client.get(reverse("accounts:dashboard")).content.decode()

    assert "Vaultwise Archive" in body


def test_a_maintainer_gets_an_edit_link_for_their_software(client, publisher, product):
    body = client.get(reverse("accounts:dashboard")).content.decode()

    assert reverse("software:propose_edit", args=[product.slug]) in body


def test_the_dashboard_shows_software_submissions_with_status(client, publisher,
                                                              product):
    SoftwareSubmission.objects.create(
        submitter=publisher, software=product,
        claimed_validation_level=ValidationLevel.COMMUNITY,
    )

    body = client.get(reverse("accounts:dashboard")).content.decode()

    assert "Pending" in body


def test_the_dashboard_shows_an_open_vendor_claim(client, publisher):
    """Otherwise a claimant has no way to see that their claim is still waiting."""
    from lumina.vendors.services import claim_vendor

    orphan = Vendor.objects.create(name="Orbital Forge", scope=Vendor.SCOPE_SOFTWARE)
    claim_vendor(
        vendor=orphan, requester=publisher, work_email="p@orbitalforge.example",
        role_at_vendor="Engineering",
    )

    body = client.get(reverse("accounts:dashboard")).content.decode()

    assert "Orbital Forge" in body


def test_someone_elses_software_is_not_listed(client, publisher, product):
    stranger = User.objects.create_user("other", email="o@example.com")
    client.force_login(stranger)

    body = client.get(reverse("accounts:dashboard")).content.decode()

    assert "Vaultwise Archive" not in body
