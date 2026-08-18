"""Socket-count documentation and filtering on System listings.

Socket count is never a validation discriminator: a run in any configuration validates the whole
system. So it is derived from what the system's public runs cited, surfaced as documentation and a
discovery filter (a multi-socket build never benchmarks at 2x a single-socket one), and it neither
raises nor lowers a certification.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.urls import reverse
from django.utils import timezone

from lumina.api.serializers import SystemSerializer
from lumina.core.certification import ValidationLevel
from lumina.hardware.filters import filter_listings
from lumina.hardware.models import Component, System
from lumina.releases.models import AlmaLinuxRelease
from lumina.results.models import RunType, TestRun
from lumina.vendors.models import Vendor

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture(autouse=True)
def release():
    return AlmaLinuxRelease.objects.get_or_create(
        major=9, defaults={"supported": True},
    )[0]


def _system(name="PowerEdge R760", vendor_name="Dell Inc.") -> System:
    vendor = Vendor.objects.create(name=vendor_name, published=True)
    return System.objects.create(
        vendor=vendor, name=name, model_number=name, published=True,
    )


def _run(system, sockets, *, public=True):
    """A run of ``system`` citing ``sockets`` physical sockets; public unless told otherwise."""
    n = TestRun.objects.count()
    return TestRun.objects.create(
        run_type=RunType.validate.value,
        schema_version="1.0", suite_version="0.1.0",
        submitter=User.objects.create_user(f"u{n}"), source="api",
        bundle=ContentFile(b"x", name=f"b{n}.tar.zst"),
        bundle_sha256=f"{n:064d}",
        status=TestRun.STATUS_APPROVED if public else TestRun.STATUS_DRAFT,
        published_at=timezone.now() if public else None,
        alma_release=AlmaLinuxRelease.objects.get(major=9),
        listing_system=system,
        cpu_sockets=sockets,
    )


def test_socket_counts_are_distinct_sorted_and_public_only():
    system = _system()
    _run(system, 2)
    _run(system, 1)
    _run(system, 2)               # a repeated config collapses
    _run(system, 4, public=False)  # a draft is not evidence
    assert system.socket_counts() == [1, 2]


def test_a_system_with_no_socket_evidence_lists_nothing():
    assert _system().socket_counts() == []


def test_filter_matches_systems_seen_in_that_socket_config():
    dual = _system("Dual", "Dell Inc.")
    single = _system("Single", "HPE")
    _run(dual, 2)
    _run(single, 1)

    assert list(filter_listings(System, params={"sockets": ["2"]})) == [dual]
    assert set(filter_listings(System, params={"sockets": ["1", "2"]})) == {dual, single}


def test_a_draft_run_does_not_make_a_system_match():
    system = _system()
    _run(system, 2, public=False)
    assert list(filter_listings(System, params={"sockets": ["2"]})) == []


def test_sockets_filter_does_not_apply_to_components():
    # The sockets branch is System-only; on Components the param is ignored, not an error.
    assert list(filter_listings(Component, params={"sockets": ["2"]})) == list(
        filter_listings(Component, params={})
    )


def test_socket_count_is_independent_of_validation_level():
    """The rule: a run in any socket config validates all. A system whose only evidence is a
    single-socket run still carries whatever level it earned; socket count does not touch it."""
    system = _system()
    system.validation_level = ValidationLevel.COMMUNITY
    system.save(update_fields=["validation_level"])
    _run(system, 1)
    system.refresh_from_db()
    assert system.socket_counts() == [1]
    assert system.validation_level == ValidationLevel.COMMUNITY


def test_detail_page_documents_the_socket_configs(client):
    system = _system()
    _run(system, 2)
    html = client.get(reverse("hardware:detail", args=[system.slug])).content.decode()
    assert "Socket configurations validated: 2" in html


def test_api_exposes_socket_counts():
    system = _system()
    _run(system, 2)
    _run(system, 1)
    ser = SystemSerializer()
    assert "socket_counts" in ser.fields
    assert ser.get_socket_counts(system) == [1, 2]
