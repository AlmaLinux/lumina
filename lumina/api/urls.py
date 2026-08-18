"""DRF API v1 URLs."""
from __future__ import annotations

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from lumina.api import device_views, result_views, survey_views, views

router = DefaultRouter()
router.register("systems", views.SystemViewSet, basename="system")
router.register("components", views.ComponentViewSet, basename="component")
router.register("software", views.SoftwareViewSet, basename="software")
router.register("categories", views.CategoryViewSet, basename="category")
router.register("vendors", views.VendorViewSet, basename="vendor")
router.register("submissions", views.SubmissionViewSet, basename="submission")
router.register("results", result_views.ResultViewSet, basename="result")
router.register("benchmarks", result_views.BenchmarkViewSet, basename="benchmark")

urlpatterns = [
    # Cheap "is my token still real" probe for the CLI's pre-run checks. Any scope may
    # call it; see the view for why.
    path("token", device_views.token_info, name="token-info"),
    path("device/code", device_views.device_code, name="device-code"),
    path("device/token", device_views.device_token, name="device-token"),
    path("survey/", survey_views.SurveyIngestView.as_view(), name="survey-ingest"),
    path("", include(router.urls)),
]
