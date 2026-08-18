"""Vendor proposal URLs (mounted at /vendors/)."""
from __future__ import annotations

from django.urls import path

from lumina.vendors import views

app_name = "vendors"

urlpatterns = [
    path("propose-new/", views.propose_new, name="propose_new"),
    path("<slug:slug>/propose-edit/", views.propose_edit, name="propose_edit"),
    path("<slug:slug>/claim/", views.claim, name="claim"),
]
