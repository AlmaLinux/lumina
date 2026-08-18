"""Browse, detail, and the one-click attestation controls.

The detail page's per-major table is the heart of the feature: one row per cited
AlmaLinux major, each with its own tier badge and its own confirmation count.

Attesting is deliberately one POST with nothing but a CSRF token and a major.
Reporting a *new* major is the one action that is not one click, because it adds a
major to somebody else's listing and gets reviewed once.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from lumina.core.certification import ValidationLevel
from lumina.releases.models import AlmaLinuxRelease
from lumina.software.models import (
    Software,
    SoftwareAttestation,
    SoftwareCertification,
    SoftwareCompatibility,
)
from lumina.vendors.models import Vendor

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture(autouse=True)
def releases():
    for major in (8, 9, 10):
        AlmaLinuxRelease.objects.get_or_create(major=major,
                                               defaults={"supported": True})


@pytest.fixture
def backup():
    """Vendor-certified on 8 and 9, community-only on 10 - the abandonment shape."""
    vendor = Vendor.objects.create(
        name="Vaultwise", scope=Vendor.SCOPE_SOFTWARE, verified=True,
    )
    product = Software.objects.create(
        vendor=vendor, name="Vaultwise Archive", published=True,
        owner_vendor=vendor,
    )
    for major in (8, 9, 10):
        row = SoftwareCompatibility.objects.create(
            software=product, release=AlmaLinuxRelease.objects.get(major=major),
        )
        if major != 10:
            SoftwareCertification.objects.create(
                compatibility=row, level=ValidationLevel.VENDOR,
            )
    product.refresh_from_db()
    return product


@pytest.fixture
def fan(client):
    user = User.objects.create_user("fan", email="fan@example.com")
    client.force_login(user)
    return user


def _attest_url(product, major):
    return reverse("software:attest", args=[product.slug, major])


# --- browse -------------------------------------------------------------------


def test_the_browse_page_lists_published_software(client, backup):
    body = client.get(reverse("software:browse")).content.decode()

    assert "Vaultwise Archive" in body


def test_the_browse_card_shows_only_the_overall_level(client, backup):
    """One badge per card, not one per cited major.

    A card carrying every major's tier made a page of listings unreadable, so the
    per-major table stays on the detail page where there is room to read it.

    Fetched as the HTMX fragment so the assertion sees the cards alone. The full
    page also carries the AlmaLinux filter checkboxes, whose labels name every
    major for reasons that have nothing to do with the card.
    """
    body = client.get(
        reverse("software:browse"), headers={"HX-Request": "true"}
    ).content.decode()

    assert "badge-vendor" in body
    assert body.count("badge-validation") == 1
    for major in (8, 9, 10):
        assert f"AlmaLinux {major}" not in body


def test_the_card_shows_the_attestation_count_like_hardware_does(client, backup):
    """Same "× N attestations" line the hardware cards carry beside the badge.

    Totalled across majors, so a product confirmed on three releases reads as
    three. ``Software`` has no denormalized count - deliberately, since the number
    is per major - so the browse view annotates it.
    """
    for row in backup.compatibility.all():
        for name in ("a", "b"):
            SoftwareAttestation.objects.create(
                compatibility=row, user=User.objects.create_user(f"{name}{row.pk}"),
            )

    body = client.get(
        reverse("software:browse"), headers={"HX-Request": "true"}
    ).content.decode()

    # Three majors x two people.
    assert "× 6 attestations" in body


def test_a_single_attestation_is_not_called_out(client, backup):
    """Matching hardware, which only shows the line above one: "× 1 attestations"
    is noise and reads badly."""
    row = backup.compatibility.first()
    SoftwareAttestation.objects.create(
        compatibility=row, user=User.objects.create_user("solo"),
    )

    body = client.get(
        reverse("software:browse"), headers={"HX-Request": "true"}
    ).content.decode()

    assert "attestations" not in body


def test_a_pending_majors_attestations_do_not_inflate_the_card(client, backup):
    """A community-reported major is invisible until a reviewer accepts it, so its
    confirmations must not show up in the public total either."""
    # Convert the fixture's approved 8 into a community report awaiting review,
    # rather than adding a second row for the same major - unique(software, release)
    # exists precisely to stop that.
    pending = backup.compatibility.get(release__major=8)
    pending.certifications.all().delete()
    pending.status = SoftwareCompatibility.STATUS_PENDING
    pending.save(update_fields=["status"])
    for name in ("x", "y", "z"):
        SoftwareAttestation.objects.create(
            compatibility=pending, user=User.objects.create_user(name),
        )
    approved = backup.compatibility.filter(
        status=SoftwareCompatibility.STATUS_APPROVED
    )
    for row in approved:
        for name in ("p", "q"):
            SoftwareAttestation.objects.create(
                compatibility=row, user=User.objects.create_user(f"{name}{row.pk}"),
            )

    body = client.get(
        reverse("software:browse"), headers={"HX-Request": "true"}
    ).content.decode()

    # Two approved majors x two people; the three on the pending major excluded.
    assert "× 4 attestations" in body


def test_a_verified_vendors_listing_does_not_advertise_a_claim(client, backup):
    """Same rule as hardware, via the shared helper - see
    ``vendors/tests/test_claim_views.py`` for why verified is not solicited."""
    body = client.get(reverse("software:detail", args=[backup.slug])).content.decode()

    assert "Are you the vendor?" not in body


def test_an_unverified_unclaimed_vendors_listing_does_advertise_a_claim(client, backup):
    backup.vendor.verified = False
    backup.vendor.save(update_fields=["verified"])

    body = client.get(reverse("software:detail", args=[backup.slug])).content.decode()

    assert "Are you the vendor?" in body


# --- detail -------------------------------------------------------------------


def test_the_detail_page_shows_one_row_per_major_with_its_own_tier(client, backup):
    body = client.get(reverse("software:detail", args=[backup.slug])).content.decode()

    assert body.count("badge-vendor") >= 2      # 8 and 9
    assert "badge-community" in body            # 10
    assert "AlmaLinux 10" in body


def test_a_pending_major_is_hidden_from_other_visitors(client, backup):
    reporter = User.objects.create_user("rep", email="rep@example.com")
    from lumina.software import services
    # 8 is already cited, so use a fresh release for the report.
    AlmaLinuxRelease.objects.get_or_create(major=11, defaults={"supported": True})
    services.report_new_major(
        software=backup, release=AlmaLinuxRelease.objects.get(major=11), user=reporter,
    )

    body = client.get(reverse("software:detail", args=[backup.slug])).content.decode()

    assert "AlmaLinux 11" not in body


def test_the_reporter_sees_their_own_pending_major(client, backup):
    from lumina.software import services
    AlmaLinuxRelease.objects.get_or_create(major=11, defaults={"supported": True})
    reporter = User.objects.create_user("rep", email="rep@example.com")
    client.force_login(reporter)
    services.report_new_major(
        software=backup, release=AlmaLinuxRelease.objects.get(major=11), user=reporter,
    )

    body = client.get(reverse("software:detail", args=[backup.slug])).content.decode()

    assert "AlmaLinux 11" in body
    assert "review" in body.lower()


def test_an_unpublished_listing_is_a_404(client, backup):
    backup.published = False
    backup.save(update_fields=["published"])

    assert client.get(reverse("software:detail", args=[backup.slug])).status_code == 404


# --- attesting ----------------------------------------------------------------


def test_an_anonymous_visitor_sees_counts_but_no_attest_control(client, backup):
    body = client.get(reverse("software:detail", args=[backup.slug])).content.decode()

    assert "Confirm it works" not in body


def test_a_logged_in_visitor_gets_the_control(client, backup, fan):
    body = client.get(reverse("software:detail", args=[backup.slug])).content.decode()

    assert "Confirm it works" in body


def test_attesting_moves_the_count(client, backup, fan):
    resp = client.post(_attest_url(backup, 10), follow=True)

    assert resp.status_code == 200
    row = backup.compatibility.get(release__major=10)
    assert row.attestations.count() == 1


def test_attesting_twice_is_not_an_error(client, backup, fan):
    client.post(_attest_url(backup, 10))
    resp = client.post(_attest_url(backup, 10), follow=True)

    assert resp.status_code == 200
    assert backup.compatibility.get(release__major=10).attestations.count() == 1


def test_the_same_user_can_confirm_a_second_major(client, backup, fan):
    client.post(_attest_url(backup, 10))
    client.post(_attest_url(backup, 9))

    assert SoftwareAttestation.objects.filter(user=fan).count() == 2


def test_withdrawing_drops_the_count(client, backup, fan):
    client.post(_attest_url(backup, 10))

    client.post(reverse("software:withdraw", args=[backup.slug, 10]), follow=True)

    assert backup.compatibility.get(release__major=10).attestations.count() == 0


def test_an_anonymous_post_cannot_attest(client, backup):
    resp = client.post(_attest_url(backup, 10))

    assert resp.status_code in (302, 403)
    assert SoftwareAttestation.objects.count() == 0


def test_an_htmx_attest_returns_just_the_row(client, backup, fan):
    """So the count updates in place instead of reloading the page."""
    resp = client.post(_attest_url(backup, 10), HTTP_HX_REQUEST="true")

    body = resp.content.decode()
    assert resp.status_code == 200
    assert "<html" not in body.lower()
    assert "AlmaLinux 10" in body


def test_attesting_an_uncited_major_is_rejected(client, backup, fan):
    AlmaLinuxRelease.objects.get_or_create(major=11, defaults={"supported": True})

    resp = client.post(_attest_url(backup, 11), follow=True)

    assert resp.status_code == 200
    assert SoftwareAttestation.objects.count() == 0


# --- reporting a new major ----------------------------------------------------


def test_the_report_control_offers_only_uncited_supported_majors(client, backup, fan):
    AlmaLinuxRelease.objects.get_or_create(major=11, defaults={"supported": True})

    body = client.get(reverse("software:detail", args=[backup.slug])).content.decode()
    options = body.split('name="release"')[1].split("</select>")[0]

    assert "11" in options
    # 8, 9, and 10 are already cited, so offering them could only create a
    # duplicate row.
    assert ">AlmaLinux 9<" not in options


def test_reporting_a_new_major_creates_a_pending_row(client, backup, fan):
    AlmaLinuxRelease.objects.get_or_create(major=11, defaults={"supported": True})

    client.post(reverse("software:report_major", args=[backup.slug]),
                {"release": "11"}, follow=True)

    row = backup.compatibility.get(release__major=11)
    assert row.status == SoftwareCompatibility.STATUS_PENDING
    assert row.proposed_by == fan
    assert row.attestations.count() == 1


# ``test_a_pending_report_does_not_change_the_listing_badge`` lived here and in
# test_services.py. Both are gone. The rule they named - an unreviewed major must not
# lift the product's badge - is real and is pinned at
# test_models.py::test_a_pending_major_does_not_lift_the_listing_badge, the only test
# repo-wide that fails when the STATUS_APPROVED guard is removed from
# ``recompute_levels``.
#
# Neither copy could discriminate it, for two reasons that compound: ``report_new_major``
# never recomputes the rollup, so the assertion compared a column the action does not
# write; and the ``backup`` fixture is already vendor-certified, and vendor is the top
# tier, so no pending row can raise its badge whatever tier it carries. An attempt to
# strengthen this copy by certifying the pending row still passed with the guard
# deleted.
#
# The HTTP plumbing they also touched is covered by the test above, which asserts the
# posted major arrives pending with the reporter's confirmation attached.


def test_an_owning_vendor_member_can_load_the_detail_page(client, backup):
    """Guards a latent NoReverseMatch: the Propose-edit link only renders for a
    user with edit rights, so an anonymous-only test suite never resolves it."""
    from lumina.vendors.models import VendorMembership

    maintainer = User.objects.create_user("maint", email="m@example.com")
    VendorMembership.objects.create(
        user=maintainer, vendor=backup.owner_vendor,
        role=VendorMembership.ROLE_SUBMITTER,
    )
    client.force_login(maintainer)

    resp = client.get(reverse("software:detail", args=[backup.slug]))

    assert resp.status_code == 200
    assert "Propose edit" in resp.content.decode()
