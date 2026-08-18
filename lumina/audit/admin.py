"""Admin registration for the audit log.

Everything is read-only; append-only rules are enforced at the model level
but the admin UI further discourages tampering.
"""
from __future__ import annotations

from django.contrib import admin
from unfold.admin import ModelAdmin

from lumina.audit.models import AuditLogEntry


@admin.register(AuditLogEntry)
class AuditLogEntryAdmin(ModelAdmin):
    list_display = ("created_at", "action", "actor", "ip", "target_content_type", "target_id")
    list_filter = ("action", "target_content_type")
    search_fields = ("action", "actor__username", "notes")
    readonly_fields = [f.name for f in AuditLogEntry._meta.fields]

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False
