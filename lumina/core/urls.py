"""Root-level URLs for the combined browse landing page.

The landing page at "/" shows systems and components together, because the question somebody
arrives with is "is my machine certified" rather than "which kind of thing is it".
"""
from django.contrib.sitemaps import views as sitemap_views
from django.urls import path

from . import views
from .sitemaps import SITEMAPS

app_name = "core"

urlpatterns = [
    path("", views.HomeView.as_view(), name="home"),
    path("robots.txt", views.robots_txt, name="robots"),
    # An index plus a section each, rather than one document: the listing sections grow with the
    # catalog and a single sitemap has a hard limit of 50,000 URLs.
    # ``sitemap_url_name`` spelled out because these URLs live in the ``core`` namespace, and the
    # index reverses the section view by name - the default name it looks for is unnamespaced and
    # would not resolve.
    path("sitemap.xml", sitemap_views.index,
         {"sitemaps": SITEMAPS, "sitemap_url_name": "core:sitemap-section"},
         name="sitemap-index"),
    path("sitemap-<section>.xml", sitemap_views.sitemap, {"sitemaps": SITEMAPS},
         name="sitemap-section"),
]
