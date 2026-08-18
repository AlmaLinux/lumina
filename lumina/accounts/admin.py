"""Admin registration for the accounts app.

API tokens are exposed read-only (minus revocation) so admins can audit
active tokens without being able to forge a usable one - the raw token
value is only available at issue time, never recoverable from the hash.
"""
from __future__ import annotations

from django.contrib import admin
from unfold.admin import ModelAdmin

from lumina.accounts.models import ApiToken


@admin.register(ApiToken)
class ApiTokenAdmin(ModelAdmin):
    list_display = ("user", "name", "scopes", "created_at", "last_used_at", "expires_at", "revoked_at")
    list_filter = ("revoked_at", "expires_at")
    search_fields = ("user__username", "name")
    autocomplete_fields = ("user",)
    readonly_fields = ("token_hash", "created_at", "last_used_at")

    # Prevent issuing tokens from the admin - they must go through
    # ApiToken.issue() so the raw value is surfaced to the user. Admins can
    # still revoke and delete.
    def has_add_permission(self, request) -> bool:
        return False
