"""Public hardware catalog URLs."""
from __future__ import annotations

from django.urls import path

from lumina.hardware import views

app_name = "hardware"

urlpatterns = [
    path("", views.systems, name="browse"),
    path("systems/", views.systems, name="systems"),
    path("components/", views.components, name="components"),
    # Before the slug catch-all, or "vendor-search" reads as a listing slug.
    path(
        "vendor-search/<str:kind>/", views.vendor_search, name="vendor_search",
    ),
    path("<slug:slug>/", views.detail, name="detail"),
    path("<slug:slug>/propose-edit/", views.propose_edit, name="propose_edit"),
]
