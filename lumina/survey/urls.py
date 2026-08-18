"""Submitter-facing survey URLs."""
from __future__ import annotations

from django.urls import path
from django.views.generic import RedirectView

from lumina.survey import views

app_name = "survey"

urlpatterns = [
    # The survey statistics moved onto the single public statistics page, where they sit
    # beside the certification totals with the difference between the two populations
    # stated. Kept as a redirect under the old name so any link out there still lands, and
    # temporary rather than permanent so it can be undone without fighting browser caches.
    path("", RedirectView.as_view(pattern_name="results:stats", permanent=False),
         name="stats"),
    path("tokens/request/", views.request_token, name="request_token"),
]
