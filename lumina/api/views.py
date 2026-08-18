"""DRF views for the public JSON API.

Listing endpoints share ``filter_listings`` with the HTML catalog so the
two surfaces can't drift. Submissions is scaffolded: authentication runs
end-to-end (Bearer token with ``submit`` scope), but the create path
itself returns 501 until the submission form UI lands.
"""
from __future__ import annotations

from rest_framework import status, viewsets
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response

from lumina.accounts.models import ApiToken
from lumina.api.serializers import (
    CategorySerializer,
    ComponentSerializer,
    SoftwareSerializer,
    SystemSerializer,
    VendorSerializer,
)
from lumina.hardware.filters import filter_listings
from lumina.hardware.models import Component, System
from lumina.software.filters import filter_software
from lumina.taxonomy.models import Category
from lumina.vendors.models import Vendor


class _ListingViewSetBase(viewsets.ReadOnlyModelViewSet):
    """Shared read-only listing endpoint backed by the catalog filter."""

    permission_classes = [AllowAny]
    lookup_field = "slug"
    listing_model: type  # subclasses set this.

    def get_queryset(self):
        return filter_listings(self.listing_model, params=dict(self.request.GET.lists())).select_related("vendor")


class SystemViewSet(_ListingViewSetBase):
    listing_model = System
    serializer_class = SystemSerializer


class ComponentViewSet(_ListingViewSetBase):
    listing_model = Component
    serializer_class = ComponentSerializer


class CategoryViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [AllowAny]
    lookup_field = "slug"
    serializer_class = CategorySerializer
    queryset = Category.objects.all().prefetch_related("values")


class VendorViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [AllowAny]
    lookup_field = "slug"
    serializer_class = VendorSerializer
    queryset = Vendor.objects.all()


class SubmissionViewSet(viewsets.ViewSet):
    """Scaffolded submission endpoint.

    Requires an authenticated request (session or Bearer token). Bearer
    tokens must additionally carry the ``submit`` scope. Until the create
    flow is implemented the endpoint returns 501 so clients can exercise
    auth end-to-end without a working payload shape to depend on.
    """

    def create(self, request: Request):
        if not request.user.is_authenticated:
            return Response({"detail": "Authentication required."}, status=status.HTTP_401_UNAUTHORIZED)
        token: ApiToken | None = request.auth if isinstance(request.auth, ApiToken) else None
        if token is not None and not token.has_scope(ApiToken.SCOPE_SUBMIT):
            return Response(
                {"detail": "Token lacks 'submit' scope."},
                status=status.HTTP_403_FORBIDDEN,
            )
        return Response(
            {"detail": "Submission API not yet implemented."},
            status=status.HTTP_501_NOT_IMPLEMENTED,
        )


class SoftwareViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only software catalog.

    ``get_queryset`` delegates to the same ``filter_software`` the HTML browse
    page uses, so a query string means the same thing on both surfaces.
    """

    serializer_class = SoftwareSerializer
    permission_classes = [AllowAny]
    lookup_field = "slug"

    def get_queryset(self):
        return filter_software(
            params=dict(self.request.GET.lists())
        ).select_related("vendor")
