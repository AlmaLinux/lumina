"""The per-release payload on /api/v1/systems/ and /api/v1/components/.

Flat functions, deliberately. When this was written ``test_api.py``'s classes were
named ``TestSystemsEndpoint`` and friends, which ``pyproject.toml``'s
``python_classes`` does not match, so all ten of its tests were silently skipped -
which is how a nested field could be added to the listing serializers with nothing
checking it. They have since been renamed and now run; a flat function cannot fall
out of collection in the first place.

What this pins down:
- every cited release appears, newest first, with its own tier and count
- the payload is majors only, the same unit the software catalog uses; it used to
  carry a ``minimum_minor`` floor and labels like "AlmaLinux 8.10+"
- a declared release reports an empty tier rather than being floored or omitted
- the listing rollup and the per-release detail disagree in the abandonment case,
  which is the reason the list exists
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from lumina.core.certification import ValidationLevel
from lumina.hardware.models import (
    CommunityAttestation,
    ListingVersion,
    Submission,
    System,
)
from lumina.hardware.services import recompute_listing_levels
from lumina.releases.models import AlmaLinuxRelease
from lumina.vendors.models import Vendor

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def abandoned_system():
    """Vendor-validated on 8, community-validated on 10, 9 merely declared."""
    for major in (8, 9, 10):
        AlmaLinuxRelease.objects.get_or_create(
            major=major, defaults={"supported": True},
        )
    vendor = Vendor.objects.create(name="Dell Inc.", published=True, verified=True)
    system = System.objects.create(
        vendor=vendor, name="PowerEdge R750", published=True,
    )
    pairs = [
        (8, ValidationLevel.VENDOR),
        (9, None),
        (10, ValidationLevel.COMMUNITY),
    ]
    for major, level in pairs:
        version = ListingVersion.objects.create(
            listing_system=system,
            release=AlmaLinuxRelease.objects.get(major=major),
            source=(
                ListingVersion.SOURCE_DECLARED if level is None
                else ListingVersion.SOURCE_RUN
            ),
        )
        if level is not None:
            # Every attestation needs exactly one source
            # (``attestation_exactly_one_source``). A submission is the cheaper of
            # the two to build here; what it is evidence *from* is not what these
            # tests are about.
            attester = User.objects.create_user(f"u{major}")
            CommunityAttestation.objects.create(
                version=version, listing_system=system, level=level,
                attested_by=attester,
                submission=Submission.objects.create(
                    submitter=attester, listing_system=system,
                    claimed_validation_level=level,
                ),
            )
    recompute_listing_levels(system)
    return system


def _payload(api, system):
    response = api.get(f"/api/v1/systems/{system.slug}/")
    assert response.status_code == 200
    return response.json()


def test_every_cited_release_appears_newest_first(api, abandoned_system):
    body = _payload(api, abandoned_system)

    assert [row["major"] for row in body["compatibility"]] == [10, 9, 8]


def test_each_release_carries_its_own_tier_and_count(api, abandoned_system):
    rows = {row["major"]: row for row in _payload(api, abandoned_system)["compatibility"]}

    assert rows[8]["validation_level"] == ValidationLevel.VENDOR
    assert rows[8]["validation_level_display"] == "Vendor-validated"
    assert rows[8]["attestation_count"] == 1
    assert rows[10]["validation_level"] == ValidationLevel.COMMUNITY
    assert rows[10]["attestation_count"] == 1


def test_the_payload_is_majors_only(api, abandoned_system):
    """It used to publish ``minimum_minor`` and labels like "AlmaLinux 8.10+".

    Hardware certifies per major now, the same unit the software catalog uses, so the field is
    removed rather than zeroed - a consumer reading ``0`` as "no floor" would keep believing
    there is a floor to read.
    """
    rows = _payload(api, abandoned_system)["compatibility"]

    assert rows, "the listing cites releases"
    for row in rows:
        assert "minimum_minor" not in row
        assert row["display"] == f"AlmaLinux {row['major']}"

def test_a_declared_release_reports_no_tier(api, abandoned_system):
    rows = {row["major"]: row for row in _payload(api, abandoned_system)["compatibility"]}

    assert rows[9]["source"] == "declared"
    assert rows[9]["validation_level"] == ""
    assert rows[9]["validation_level_display"] == ""
    assert rows[9]["attestation_count"] == 0


def test_the_rollup_hides_what_the_per_release_list_shows(api, abandoned_system):
    """The reason a client has to read ``compatibility`` rather than the badge.

    The listing reads Vendor-validated on the strength of AlmaLinux 8 alone, while
    the only validation on 10 is from the community.
    """
    body = _payload(api, abandoned_system)

    assert body["validation_level"] == ValidationLevel.VENDOR
    assert body["attestation_count"] == 2
    rows = {row["major"]: row for row in body["compatibility"]}
    assert rows[10]["validation_level"] == ValidationLevel.COMMUNITY
