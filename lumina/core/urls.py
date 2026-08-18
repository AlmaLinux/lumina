"""Root-level URLs for the combined browse landing page.

The landing page at "/" shows both systems and components, mirroring
catalog.redhat.com/en/search?searchType=Hardware.
"""
from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("", views.HomeView.as_view(), name="home"),
]
