"""The public software API.

Mirrors the HTML: same filter function, so JSON and the browse page cannot drift.

The rule that matters most here is that a pending community-reported major must
not leak. The HTML hides it, and an API that exposed it would make the review gate
decorative.
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
    for major in (9, 10, 11):
        AlmaLinuxRelease.objects.get_or_create(major=major,
                                               defaults={"supported": True})


@pytest.fixture
def product():
    vendor = Vendor.objects.create(name="Vaultwise", scope=Vendor.SCOPE_SOFTWARE)
    software = Software.objects.create(
        vendor=vendor, name="Vaultwise Archive", published=True,
        homepage_url="https://example.com/v",
    )
    nine = SoftwareCompatibility.objects.create(
        software=software, release=AlmaLinuxRelease.objects.get(major=9),
    )
    SoftwareCertification.objects.create(compatibility=nine,
                                         level=ValidationLevel.VENDOR)
    SoftwareAttestation.objects.create(
        compatibility=nine,
        user=User.objects.create_user("fan", email="f@example.com"),
    )
    SoftwareCompatibility.objects.create(
        software=software, release=AlmaLinuxRelease.objects.get(major=10),
    )
    software.refresh_from_db()
    return software


def test_the_list_endpoint_returns_published_software(client, product):
    payload = client.get(reverse("software-list")).json()

    assert payload["count"] == 1
    assert payload["results"][0]["name"] == "Vaultwise Archive"


def test_unpublished_software_is_absent(client, product):
    product.published = False
    product.save(update_fields=["published"])

    assert client.get(reverse("software-list")).json()["count"] == 0


def test_the_detail_endpoint_is_keyed_on_slug(client, product):
    payload = client.get(reverse("software-detail", args=[product.slug])).json()

    assert payload["slug"] == product.slug


def test_the_payload_carries_the_per_major_breakdown(client, product):
    payload = client.get(reverse("software-detail", args=[product.slug])).json()

    by_major = {row["major"]: row for row in payload["compatibility"]}
    assert by_major[9]["validation_level"] == "vendor"
    assert by_major[9]["certifications"] == ["vendor"]
    assert by_major[9]["attestation_count"] == 1
    assert by_major[10]["validation_level"] == "community"
    assert by_major[10]["certifications"] == []


def test_a_pending_reported_major_never_appears(client, product):
    """The HTML hides it; an API that exposed it would make review decorative."""
    from lumina.software import services

    reporter = User.objects.create_user("rep", email="rep@example.com")
    services.report_new_major(
        software=product, release=AlmaLinuxRelease.objects.get(major=11),
        user=reporter,
    )

    payload = client.get(reverse("software-detail", args=[product.slug])).json()

    assert 11 not in {row["major"] for row in payload["compatibility"]}


def test_the_api_uses_the_same_filters_as_the_html(client, product):
    assert client.get(reverse("software-list"), {"alma": "9"}).json()["count"] == 1
    assert client.get(reverse("software-list"), {"alma": "11"}).json()["count"] == 0
    assert client.get(
        reverse("software-list"), {"license": "open-source"}
    ).json()["count"] == 1


def test_internal_fields_stay_out_of_the_payload(client, product):
    """The serializer docstring's rule: no reviewer notes, no submitter identity,
    and no identities of the people who attested."""
    payload = client.get(reverse("software-detail", args=[product.slug])).json()

    flat = str(payload)
    for leaked in ("reviewer_notes", "submitter", "proposed_by", "created_by", "fan"):
        assert leaked not in flat
