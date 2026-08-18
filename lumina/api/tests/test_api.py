"""Tests for the public JSON API.

- /api/v1/systems/ and /api/v1/components/ return only published listings.
- Filtering uses the same composition as the HTML catalog
  (vendor + category-slug keys); we don't duplicate filter logic in the API.
- /api/v1/categories/ and /api/v1/vendors/ expose the admin-curated
  taxonomy and vendors for clients building their own filter UIs.
- Unauthenticated reads are allowed.
- POST /api/v1/submissions/ requires a Bearer token with the ``submit``
  scope; a read-only token is rejected. Tokens are validated via the
  ApiTokenAuthentication class.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from lumina.accounts.models import ApiToken
from lumina.hardware.models import Component, System
from lumina.taxonomy.models import Category, CategoryValue
from lumina.vendors.models import Vendor

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def dell():
    return Vendor.objects.create(name="Dell")


def _publish(obj):
    obj.published = True
    obj.save()
    return obj


@pytest.fixture
def dell_system(dell):
    return _publish(System.objects.create(name="PowerEdge R750", vendor=dell, model_number="R750"))


@pytest.fixture
def unpublished_system(dell):
    return System.objects.create(name="Draft", vendor=dell, model_number="x")


class SystemsEndpointTests:
    def test_lists_published(self, api, dell_system, unpublished_system):
        resp = api.get("/api/v1/systems/")
        assert resp.status_code == 200
        names = [r["name"] for r in resp.json()["results"]]
        assert "PowerEdge R750" in names
        assert "Draft" not in names

    def test_filter_by_vendor(self, api, dell, dell_system):
        hpe = Vendor.objects.create(name="HPE")
        _publish(System.objects.create(name="DL380", vendor=hpe, model_number="DL380"))
        resp = api.get("/api/v1/systems/", {"vendor": "dell"})
        names = [r["name"] for r in resp.json()["results"]]
        assert names == ["PowerEdge R750"]

    def test_anonymous_allowed(self, api, dell_system):
        assert api.get("/api/v1/systems/").status_code == 200


class ComponentsEndpointTests:
    def test_lists_published(self, api, dell):
        _publish(Component.objects.create(name="NIC", vendor=dell, model_number="BCM57414"))
        resp = api.get("/api/v1/components/")
        assert resp.status_code == 200
        assert resp.json()["count"] == 1


class TaxonomyEndpointsTests:
    def test_categories_returns_only_approved_values(self, api):
        cat = Category.objects.create(name="Arch", slug="architecture")
        CategoryValue.objects.create(category=cat, value="x86_64")
        proposer = User.objects.create_user(username="p")
        CategoryValue.propose(category=cat, value="riscv64", proposed_by=proposer)

        resp = api.get("/api/v1/categories/")
        assert resp.status_code == 200
        payload = resp.json()["results"][0]
        value_strings = [v["value"] for v in payload["values"]]
        assert "x86_64" in value_strings
        assert "riscv64" not in value_strings

    def test_vendors_endpoint(self, api, dell):
        resp = api.get("/api/v1/vendors/")
        assert resp.status_code == 200
        # Asserted by presence, not by count. Data migrations seed the CPU and GPU
        # family vendors (Intel, AMD, NVIDIA), so a total is a moving target that
        # says nothing about whether the endpoint works.
        slugs = {row["slug"] for row in resp.json()["results"]}
        assert dell.slug in slugs


class SubmissionTokenAuthTests:
    def test_no_token_rejected(self, api):
        resp = api.post("/api/v1/submissions/", {}, format="json")
        assert resp.status_code in (401, 403)

    def test_read_scope_token_rejected(self, api):
        user = User.objects.create_user(username="u")
        _, raw = ApiToken.issue(user=user, name="read-only", scopes=[ApiToken.SCOPE_READ])
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
        resp = api.post("/api/v1/submissions/", {}, format="json")
        assert resp.status_code == 403

    def test_submit_scope_token_accepted(self, api):
        user = User.objects.create_user(username="u")
        _, raw = ApiToken.issue(
            user=user, name="ci",
            scopes=[ApiToken.SCOPE_READ, ApiToken.SCOPE_SUBMIT],
        )
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
        resp = api.post("/api/v1/submissions/", {}, format="json")
        # Endpoint is a stub for v1 - it should accept the auth and return
        # 501 Not Implemented rather than rejecting us at the auth layer.
        assert resp.status_code == 501

    def test_invalid_token_rejected(self, api):
        api.credentials(HTTP_AUTHORIZATION="Bearer not-a-real-token")
        resp = api.post("/api/v1/submissions/", {}, format="json")
        assert resp.status_code in (401, 403)
