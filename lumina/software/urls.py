"""Public software catalog URLs (mounted at /software/)."""
from __future__ import annotations

from django.urls import path

from lumina.software import views

app_name = "software"

urlpatterns = [
    path("", views.browse, name="browse"),
    # Both before the slug catch-all, or they are read as listing slugs.
    path("submit/", views.submit, name="submit"),
    path("revise/<uuid:uuid>/", views.revise, name="revise"),
    path("vendor-search/", views.vendor_search, name="vendor_search"),
    path("<slug:slug>/", views.detail, name="detail"),
    path("<slug:slug>/confirm/<int:major>/", views.attest, name="attest"),
    path("<slug:slug>/withdraw/<int:major>/", views.withdraw, name="withdraw"),
    path("<slug:slug>/report-release/", views.report_major, name="report_major"),
    path("<slug:slug>/propose-edit/", views.propose_edit, name="propose_edit"),
]
