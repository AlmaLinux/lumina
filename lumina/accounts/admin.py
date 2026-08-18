"""Admin registration for the accounts app.

API tokens are exposed read-only (minus revocation) so admins can audit
active tokens without being able to forge a usable one - the raw token
value is only available at issue time, never recoverable from the hash.
"""
from __future__ import annotations

from django.conf import settings
from django.contrib import admin
from django.contrib.auth.admin import GroupAdmin as BaseGroupAdmin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import Group, User
from django.utils.html import format_html
from unfold.admin import ModelAdmin
from unfold.forms import AdminPasswordChangeForm, UserChangeForm, UserCreationForm

from lumina.accounts.models import AccountSettings, ApiToken


def _oidc_managed_groups() -> list[str]:
    """The Django groups the OIDC login controls: the values of the group map. Editing a user's
    membership of these by hand is pointless - ``lumina.accounts.auth`` overwrites them from
    Keycloak on the user's next sign-in (see ``_sync_groups``). Unmanaged groups are left alone."""
    return sorted(set((settings.LUMINA_OIDC_GROUP_MAP or {}).values()))


def _oidc_is_configured() -> bool:
    """Whether OIDC sign-in is actually wired up, so the warning is silent on a devstack host using
    the password login (which has no OIDC endpoints and never runs the group sync)."""
    return bool(getattr(settings, "OIDC_OP_AUTHORIZATION_ENDPOINT", ""))


@admin.register(AccountSettings)
class AccountSettingsAdmin(ModelAdmin):
    """Read-mostly. An admin may need to answer "why is this run listed as Anonymous",
    and the answer is sometimes this row rather than anything on the run."""

    list_display = ("user", "publish_anonymously", "updated_at")
    list_filter = ("publish_anonymously",)
    search_fields = ("user__username",)
    autocomplete_fields = ("user",)
    readonly_fields = ("updated_at",)


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


# --- auth.User / auth.Group --------------------------------------------------------
# Every model in the project registers with Unfold's ``ModelAdmin``, but ``User`` and
# ``Group`` come pre-registered by ``django.contrib.auth`` against plain
# ``admin.ModelAdmin``, so nothing here had ever touched them. That left them the only
# two changelists in the admin running Django's own ``ActionForm``, and Unfold's action
# bar cannot drive it: the bar renders the submit button as
# ``<button x-show="action">`` against an Alpine scope of ``{action: ''}``, and it is
# Unfold's ``ActionForm`` that carries the ``x-model="action"`` binding filling that in.
# Django's form has no such attribute, so ``action`` stayed empty forever and the button
# stayed hidden. Selecting users and choosing "Delete selected users" produced an action
# bar with no way to run it - the two models where bulk actions matter most.
# Re-registering with Unfold in the MRO also picks up its styled password and
# permission widgets on the change form, which had the same origin.
admin.site.unregister(User)
admin.site.unregister(Group)


@admin.register(User)
class UserAdmin(BaseUserAdmin, ModelAdmin):
    form = UserChangeForm
    add_form = UserCreationForm
    change_password_form = AdminPasswordChangeForm
    list_display = (*BaseUserAdmin.list_display, "sign_in_source")

    @admin.display(description="Sign-in")
    def sign_in_source(self, obj) -> str:
        """An account with no usable password authenticates externally - it was provisioned by the
        OIDC login, never given a local password. That is the account whose groups Keycloak owns."""
        return "OIDC (external)" if not obj.has_usable_password() else "Local password"

    def _is_oidc_account(self, user) -> bool:
        return _oidc_is_configured() and user is not None and not user.has_usable_password()

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        # Warn, on exactly the fields the login overwrites, that hand edits will not stick for an
        # OIDC account. Not a hard lock: the same form legitimately assigns *unmanaged* groups,
        # which the sync never touches, so disabling the widget would take away a real ability.
        if self._is_oidc_account(obj):
            managed = ", ".join(_oidc_managed_groups())
            note = format_html(
                "<strong>This account signs in through OIDC (Keycloak).</strong> Its groups "
                "<em>{}</em>, and staff/superuser status, are set from the identity provider on "
                "every login - changes made here are overwritten at the next sign-in. Assign these "
                "in Keycloak. Other (unmanaged) groups set here are left alone.",
                managed or "(none configured)",
            )
            for name in ("groups", "is_staff", "is_superuser"):
                field = form.base_fields.get(name)
                if field is not None:
                    field.help_text = (
                        format_html("{} {}", field.help_text, note)
                        if field.help_text else note
                    )
        return form


@admin.register(Group)
class GroupAdmin(BaseGroupAdmin, ModelAdmin):
    pass
