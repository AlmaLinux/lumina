"""Tests for the public catalog browse views.

Behavior pinned down:

- /hardware/systems/ and /hardware/components/ are public (no auth).
- Each page renders only listings of its kind.
- Query-string filters go through ``filter_listings`` (single source of
  truth) so the HTML view matches the API.
- HTMX-triggered GETs return just the results partial, letting the filter
  panel update the grid without a full page reload.
- Detail page at /hardware/<slug>/ works for both System and Component
  slugs and 404s when no published listing matches.
- Unpublished detail slugs 404 for anonymous users.
"""
from __future__ import annotations

import pytest
from django.urls import reverse

from lumina.hardware.models import Component, System
from lumina.taxonomy.models import Category, CategoryValue
from lumina.vendors.models import Vendor

pytestmark = pytest.mark.django_db


@pytest.fixture
def dell():
    return Vendor.objects.create(name="Dell")


@pytest.fixture
def hpe():
    return Vendor.objects.create(name="HPE")


def _publish(obj):
    obj.published = True
    obj.save()
    return obj


@pytest.fixture
def dell_system(dell):
    return _publish(System.objects.create(name="PowerEdge R750", vendor=dell, model_number="R750"))


@pytest.fixture
def hpe_system(hpe):
    return _publish(System.objects.create(name="ProLiant DL380", vendor=hpe, model_number="DL380"))


@pytest.fixture
def dell_component(dell):
    return _publish(Component.objects.create(name="BCM57414 NIC", vendor=dell, model_number="BCM57414"))


class SystemsBrowseTests:
    def test_lists_published_systems(self, client, dell_system, hpe_system):
        resp = client.get(reverse("hardware:systems"))
        assert resp.status_code == 200
        assert b"PowerEdge R750" in resp.content
        assert b"ProLiant DL380" in resp.content

    def test_filters_by_vendor_slug(self, client, dell_system, hpe_system):
        resp = client.get(reverse("hardware:systems"), {"vendor": "dell"})
        assert b"PowerEdge R750" in resp.content
        assert b"ProLiant DL380" not in resp.content

    def test_does_not_show_components(self, client, dell_system, dell_component):
        resp = client.get(reverse("hardware:systems"))
        assert b"PowerEdge R750" in resp.content
        assert b"BCM57414" not in resp.content

    def test_htmx_request_returns_partial(self, client, dell_system):
        resp = client.get(
            reverse("hardware:systems"), HTTP_HX_REQUEST="true"
        )
        assert resp.status_code == 200
        # Partial excludes the base layout's <header>/<main> framing.
        assert b"<header>" not in resp.content
        assert b"PowerEdge R750" in resp.content


class ComponentsBrowseTests:
    def test_lists_published_components(self, client, dell_component):
        resp = client.get(reverse("hardware:components"))
        assert resp.status_code == 200
        assert b"BCM57414" in resp.content

    def test_does_not_show_systems(self, client, dell_system, dell_component):
        resp = client.get(reverse("hardware:components"))
        assert b"PowerEdge R750" not in resp.content
        assert b"BCM57414" in resp.content


class DetailTests:
    def test_published_system_detail(self, client, dell_system):
        resp = client.get(reverse("hardware:detail", args=[dell_system.slug]))
        assert resp.status_code == 200
        assert b"PowerEdge R750" in resp.content

    def test_published_component_detail(self, client, dell_component):
        resp = client.get(reverse("hardware:detail", args=[dell_component.slug]))
        assert resp.status_code == 200
        assert b"BCM57414" in resp.content

    def test_unpublished_404(self, client, dell):
        draft = System.objects.create(name="Draft", vendor=dell, model_number="x")
        resp = client.get(reverse("hardware:detail", args=[draft.slug]))
        assert resp.status_code == 404

    def test_unknown_slug_404(self, client):
        resp = client.get(reverse("hardware:detail", args=["nothing-here"]))
        assert resp.status_code == 404


class VendorFilterPanelTests:
    def test_vendor_filter_group_rendered_with_vendors(self, client, dell_system, hpe_system):
        resp = client.get(reverse("hardware:systems"))
        # Vendor filter card should appear and list the vendors that have
        # at least one published listing of this kind.
        assert b"data-category=\"vendor\"" in resp.content
        assert b"Dell" in resp.content
        assert b"HPE" in resp.content

    def test_vendor_filter_shows_vendors_only_for_this_kind(self, client, dell_system, dell_component):
        # A vendor that owns only components must not appear in the vendor card on
        # /systems/. Dell is present either way via dell_system, so the discriminating
        # case is a vendor with nothing but a component.
        orphan = Vendor.objects.create(name="OrphanVendor")
        _publish(Component.objects.create(name="OC", vendor=orphan, model_number="OC1"))
        resp = client.get(reverse("hardware:systems"))
        assert b"OrphanVendor" not in resp.content
        resp = client.get(reverse("hardware:components"))
        assert b"OrphanVendor" in resp.content


class FilterPanelContextTests:
    def test_filter_panel_only_shows_approved_values(self, client, dell_system):
        arch = Category.objects.create(name="Architecture", slug="architecture")
        CategoryValue.objects.create(category=arch, value="x86_64")
        from django.contrib.auth import get_user_model
        User = get_user_model()
        proposer = User.objects.create_user(username="p")
        CategoryValue.propose(category=arch, value="riscv64", proposed_by=proposer)

        resp = client.get(reverse("hardware:systems"))
        assert b"x86_64" in resp.content
        assert b"riscv64" not in resp.content

    def test_filter_panel_respects_applies_to(self, client, dell_system):
        # A component-only category must not appear on the Systems page. Both
        # categories need at least one approved value; empty categories are
        # hidden from the filter panel regardless of applies_to.
        ff = Category.objects.create(
            name="Form factor", slug="form-factor",
            applies_to=Category.APPLIES_COMPONENT,
        )
        CategoryValue.objects.create(category=ff, value="M.2")
        arch = Category.objects.create(
            name="Architecture", slug="architecture",
            applies_to=Category.APPLIES_BOTH,
        )
        CategoryValue.objects.create(category=arch, value="x86_64")
        resp = client.get(reverse("hardware:systems"))
        assert b"Architecture" in resp.content
        assert b"Form factor" not in resp.content
