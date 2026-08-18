"""Public benchmark leaderboard URLs (mounted at /benchmarks/)."""
from __future__ import annotations

from django.urls import path, re_path

from lumina.results import views

app_name = "benchmarks"

urlpatterns = [
    path("", views.benchmark_index, name="index"),
    # Before the benchmark_id catch-all, which would otherwise swallow them.
    path("compare/", views.benchmark_compare, name="compare"),
    path("scores/", views.benchmark_scores, name="scores"),
    re_path(r"^(?P<benchmark_id>[\w.\-]+)/$", views.leaderboard, name="leaderboard"),
]
