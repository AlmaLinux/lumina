from __future__ import annotations

from django.apps import AppConfig


class SoftwareConfig(AppConfig):
    name = "lumina.software"
    label = "software"
    default_auto_field = "django.db.models.BigAutoField"
