"""Raising one AlmaLinux release of a product above the community tier.

Asked as: is there a way for somebody who can certify on behalf of AlmaLinux to take a software
listing from community to AlmaLinux-validated? There was not. The tier is derived from
``SoftwareCertification`` rows, and nothing outside Django admin could write one.

The first attempt routed a "Validate this listing" button to the submission form with the
listing prefilled. It worked and it was clunky - the whole product form, name and publisher and
description and every release, to say one thing about one release - and the level it carried
then applied to every major the submission cited. So the control lives on the release's own row
instead, beside "Confirm it works", which has always been a one-click action with no queue.

That shape is the point: a tier is a fact about *this product on this release*, so the control
is per release, and it names the level it will record because a one-click control has no
dropdown to ask with.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import AnonymousUser, Group, User
from django.urls import reverse

from lumina.core.certification import ValidationLevel
from lumina.releases.models import AlmaLinuxRelease
from lumina.software.models import (
    Software,
    SoftwareCertification,
    SoftwareCompatibility,
)
from lumina.software.services import certification_level_for
from lumina.vendors.models import Vendor, VendorMembership

pytestmark = pytest.mark.django_db


@pytest.fixture
def release():
    return AlmaLinuxRelease.objects.get_or_create(
        major=9, defaults={"supported": True})[0]


@pytest.fixture
def publisher():
    return Vendor.objects.create(
        name="Revalidate Co", scope=Vendor.SCOPE_SOFTWARE, published=True, verified=True)


@pytest.fixture
def listing(publisher, release):
    software = Software.objects.create(
        name="Widgetd", vendor=publisher, published=True,
        validation_level=ValidationLevel.COMMUNITY,
    )
    SoftwareCompatibility.objects.create(
        software=software, release=release,
        status=SoftwareCompatibility.STATUS_APPROVED,
    )
    return software


def certifier() -> User:
    user = User.objects.create_user("sw-certifier", password="pw")
    user.groups.add(Group.objects.get_or_create(name="certifier")[0])
    return user


def community() -> User:
    return User.objects.create_user("sw-community", password="pw")


def member_of(vendor: Vendor, username: str = "sw-vendor") -> User:
    user = User.objects.create_user(username, password="pw")
    VendorMembership.objects.create(
        user=user, vendor=vendor, role=VendorMembership.ROLE_SUBMITTER)
    return user


def second_major() -> AlmaLinuxRelease:
    return AlmaLinuxRelease.objects.get_or_create(
        major=10, defaults={"supported": True})[0]


# --- who may, and at what level --------------------------------------------------


def test_a_foundation_certifier_validates_as_almalinux(listing):
    assert certification_level_for(certifier(), listing) == ValidationLevel.ALMALINUX


def test_a_community_member_may_not(listing):
    """They already have the control that matches what they can grant: "Confirm it works" is
    exactly one community confirmation."""
    assert certification_level_for(community(), listing) == ""


def test_an_anonymous_visitor_may_not(listing):
    assert certification_level_for(AnonymousUser(), listing) == ""


def test_the_products_own_verified_publisher_validates_as_the_vendor(listing, publisher):
    assert certification_level_for(member_of(publisher), listing) == ValidationLevel.VENDOR


def test_the_vendor_maintaining_the_listing_may_too(listing):
    """Publisher or maintainer, the pair hardware's ``_listing_belongs_to`` accepts: a product
    the community catalogued and somebody later took over carries a maintainer who is not the
    publisher, and they are as entitled to validate it."""
    maintainer = Vendor.objects.create(
        name="Maintainer GmbH", scope=Vendor.SCOPE_SOFTWARE, published=True, verified=True)
    listing.owner_vendor = maintainer
    listing.save(update_fields=["owner_vendor"])

    assert certification_level_for(member_of(maintainer), listing) == ValidationLevel.VENDOR


def test_a_member_of_some_other_vendor_may_not(listing):
    """"Vendor-validated" means the company that makes the thing validated it. Belonging to a
    verified vendor is not standing to say that about a product they do not publish."""
    other = Vendor.objects.create(
        name="Unrelated Ltd", scope=Vendor.SCOPE_SOFTWARE, published=True, verified=True)

    assert certification_level_for(member_of(other), listing) == ""


def test_an_unverified_publishers_member_may_not(listing, publisher):
    """Verification is the SIG saying this person's company is who they say. Without it the
    vendor tier is not theirs to claim."""
    publisher.verified = False
    publisher.save(update_fields=["verified"])

    assert certification_level_for(member_of(publisher), listing) == ""


def test_speaking_for_the_vendor_outranks_speaking_for_the_foundation(listing, publisher):
    """Both apply to a certifier who also works there. Vendor is the stronger and more specific
    statement, which is the order ``resolve_claimed_level`` already uses."""
    user = member_of(publisher)
    user.groups.add(Group.objects.get_or_create(name="certifier")[0])

    assert certification_level_for(user, listing) == ValidationLevel.VENDOR


# --- the control, on the release's row -------------------------------------------


def test_the_release_row_offers_it_to_a_certifier(client, listing):
    client.force_login(certifier())

    body = client.get(listing.get_absolute_url()).content.decode()

    assert "Validate as AlmaLinux-validated" in body
    assert reverse("software:certify", args=[listing.slug, 9]) in body


def test_the_button_names_the_vendor_whose_validation_it_records(client, listing, publisher):
    """"Validate as Vendor" says nothing. Whose word is being recorded is the whole content of
    the claim."""
    client.force_login(member_of(publisher))

    body = client.get(listing.get_absolute_url()).content.decode()

    assert "Validate as Revalidate Co" in body


def test_the_release_row_does_not_offer_it_to_everybody(client, listing):
    client.force_login(community())

    body = client.get(listing.get_absolute_url()).content.decode()

    assert "Validate as" not in body
    assert "Confirm it works" in body, "the community control is still there"


def test_a_release_already_carrying_that_level_is_not_offered_it_again(client, listing):
    user = certifier()
    client.force_login(user)
    client.post(reverse("software:certify", args=[listing.slug, 9]))

    body = client.get(listing.get_absolute_url()).content.decode()

    assert "Validate as" not in body


# --- what it records -------------------------------------------------------------


def test_one_click_validates_that_release(client, listing, release):
    client.force_login(certifier())

    response = client.post(reverse("software:certify", args=[listing.slug, 9]))

    assert response.status_code == 302
    listing.refresh_from_db()
    assert listing.validation_level == ValidationLevel.ALMALINUX
    row = listing.compatibility.get(release=release)
    assert row.validation_level == ValidationLevel.ALMALINUX
    assert row.certifications.get().certified_by.username == "sw-certifier"


def test_it_leaves_the_other_releases_alone(client, listing):
    """The reason the control is per release. Validating 10 must not make a statement about 9,
    which this person did not test."""
    SoftwareCompatibility.objects.create(
        software=listing, release=second_major(),
        status=SoftwareCompatibility.STATUS_APPROVED,
    )
    client.force_login(certifier())

    client.post(reverse("software:certify", args=[listing.slug, 10]))

    levels = {
        row.release.major: row.validation_level
        for row in listing.compatibility.select_related("release")
    }
    assert levels[10] == ValidationLevel.ALMALINUX
    assert levels[9] != ValidationLevel.ALMALINUX


def test_clicking_twice_records_one_validation(client, listing):
    """A double click is an accident, and the database already says so."""
    client.force_login(certifier())
    url = reverse("software:certify", args=[listing.slug, 9])

    client.post(url)
    client.post(url)

    assert SoftwareCertification.objects.count() == 1


def test_a_community_member_posting_it_directly_is_refused(client, listing):
    """The control's absence is not the gate; the service is. A button that is merely hidden
    is not a permission."""
    client.force_login(community())

    client.post(reverse("software:certify", args=[listing.slug, 9]))

    assert SoftwareCertification.objects.count() == 0
    listing.refresh_from_db()
    assert listing.validation_level == ValidationLevel.COMMUNITY


def test_an_anonymous_post_is_sent_to_sign_in(client, listing):
    response = client.post(reverse("software:certify", args=[listing.slug, 9]))

    assert response.status_code == 302
    assert SoftwareCertification.objects.count() == 0


def test_an_unpublished_listing_has_no_row_to_validate(client, listing):
    listing.published = False
    listing.save(update_fields=["published"])
    client.force_login(certifier())

    response = client.post(reverse("software:certify", args=[listing.slug, 9]))

    assert response.status_code == 404


def test_the_row_comes_back_updated_for_htmx(client, listing):
    """It is an HTMX control, so the answer is the row itself - with the new tier on it and the
    control gone, since that level is now recorded."""
    client.force_login(certifier())

    response = client.post(
        reverse("software:certify", args=[listing.slug, 9]), HTTP_HX_REQUEST="true")

    body = response.content.decode()
    assert response.status_code == 200
    assert "AlmaLinux" in body
    assert "Validate as" not in body, "the control must not reappear beside what it recorded"
