"""User dashboard URLs (/my/)."""
from __future__ import annotations

from django.urls import path

from lumina.accounts import views

app_name = "accounts"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("activate/", views.activate, name="activate"),
    path("activate/<int:pk>/confirm/", views.activate_confirm, name="activate_confirm"),
    path("tokens/", views.tokens, name="tokens"),
    path("tokens/<int:pk>/revoke/", views.token_revoke, name="token_revoke"),
]
