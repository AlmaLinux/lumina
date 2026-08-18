"""Site-wide display settings, editable without a deploy."""
from __future__ import annotations

from django.contrib import admin
from unfold.admin import ModelAdmin

from lumina.core.models import DisplayDefaults


@admin.register(DisplayDefaults)
class DisplayDefaultsAdmin(ModelAdmin):
    """One row. How much of a long list a page shows before it offers a next page."""

    list_display = ("rows_per_page", "updated_at")
    readonly_fields = ("updated_at",)

    def has_add_permission(self, request) -> bool:
        # The row is created on first read and there is only ever one of it, so an Add button
        # offers something that cannot happen.
        return not DisplayDefaults.objects.exists()

    def has_delete_permission(self, request, obj=None) -> bool:
        return False
