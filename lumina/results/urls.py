"""Public and submitter-facing result URLs."""
from __future__ import annotations

from django.urls import path

from lumina.results import download_views, feeds, views

app_name = "results"

urlpatterns = [
    path("upload/", views.upload, name="upload"),
    path("runs/<uuid:uuid>/", views.run_detail, name="run_detail"),
    # Raw evidence, gated to the submitter and reviewers (never public - carries identity).
    path("runs/<uuid:uuid>/bundle/", download_views.download_bundle, name="download_bundle"),
    path("runs/<uuid:uuid>/artifacts/<int:artifact_id>/",
         download_views.download_artifact, name="download_artifact"),
    path(
        "runs/<uuid:uuid>/submit/",
        views.submit_run_for_review,
        name="submit_for_review",
    ),
    path(
        "runs/<uuid:uuid>/submit-group/",
        views.submit_run_group_for_review,
        name="submit_group_for_review",
    ),
    path(
        "runs/<uuid:uuid>/propose-listing/",
        views.propose_listing,
        name="propose_listing",
    ),
    path("validations/latest/", views.latest_validations, name="latest_validations"),
    path("stats/", views.stats, name="stats"),
    path("runs/<uuid:uuid>/reassess/", views.reassess_run, name="reassess_run"),
    path("runs/<uuid:uuid>/anonymity/", views.set_run_anonymity, name="set_run_anonymity"),
    path("runs/<uuid:uuid>/archive/", views.archive_run, name="archive_run"),
    path("runs/<uuid:uuid>/unarchive/", views.unarchive_run, name="unarchive_run"),
    path("feeds/validations.xml", feeds.ValidationFeed(), name="validations_feed"),
    path("feeds/benchmarks.xml", feeds.BenchmarkFeed(), name="benchmarks_feed"),
]
