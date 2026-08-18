"""Authentication backends.

``LuminaOIDCBackend`` extends mozilla-django-oidc to sync Keycloak group
membership onto the Django user's groups on each login, using the map in
``settings.LUMINA_OIDC_GROUP_MAP``. Only groups that appear in the map are
touched - unrelated Django group memberships are preserved.

``ApiTokenAuthentication`` is the DRF auth class that resolves a
``Authorization: Bearer <raw>`` header to an ``ApiToken`` + user pair.
"""
from __future__ import annotations

from typing import Any, override

from django.conf import settings
from django.contrib.auth.models import Group
from django.contrib.auth.validators import UnicodeUsernameValidator
from django.core.exceptions import ValidationError
from django.utils import timezone
from mozilla_django_oidc.auth import OIDCAuthenticationBackend, default_username_algo
from rest_framework import authentication, exceptions
from rest_framework.request import Request

from lumina.accounts.models import ApiToken

_USERNAME_VALIDATOR = UnicodeUsernameValidator()
# Django's own field limit. A Keycloak username longer than this cannot be stored.
_USERNAME_MAX = 150


def username_from_claims(email: str | None, claims: dict[str, Any] | None = None) -> str:
    """The Django username for a Keycloak account: its own username.

    mozilla-django-oidc's default is a base64 SHA-224 of the email address, on the reasoning that
    usernames are often public identifiers and an email address should not be. That is sound for a
    provider that gives you nothing better, and wrong here: Keycloak sends ``preferred_username``,
    which *is* the account's name and is no more sensitive than the person's own login. The default
    put a 38-character hash in the navigation bar where a name belongs, and it looked enough like an
    opaque database key to be reported as one.

    Falls back to the hash rather than inventing something when the claim is absent, empty, too long
    for the field, or contains characters the username validator rejects. Nothing here may raise:
    this runs inside the login, and a bad claim should cost a pretty username, not the session.
    """
    candidate = ((claims or {}).get("preferred_username") or "").strip()
    if candidate and len(candidate) <= _USERNAME_MAX:
        try:
            _USERNAME_VALIDATOR(candidate)
        except ValidationError:
            pass
        else:
            return candidate
    return default_username_algo(email, claims)


class LuminaOIDCBackend(OIDCAuthenticationBackend):
    """OIDC backend that syncs Keycloak groups into Django groups."""

    def _sync_groups(self, user, claims: dict[str, Any]) -> None:
        group_map: dict[str, str] = settings.LUMINA_OIDC_GROUP_MAP
        if not group_map:
            return
        # Keycloak sends group *paths*, and what they look like depends on a checkbox in the
        # mapper. With "Full group path" off it is "admins"; with it on, "/admins", and for a nested
        # group "/almalinux/admins". Both the whole path and its last segment are matched, so the
        # map works whichever way that checkbox is set and whether or not the group is nested.
        #
        # Matching the last segment means a group at any depth named "admins" maps to Django's
        # "admin", which for a realm the deployment controls is the point: the alternative is a
        # sign-in that succeeds and grants nothing, with no error anywhere to explain it. Read it as
        # "group names are meaningful within the realm", and if that is not true of yours, key the
        # map on full paths and drop the last-segment line below.
        kc_groups: set[str] = set()
        for raw in claims.get("groups") or []:
            path = raw.lstrip("/")
            kc_groups.add(path)
            kc_groups.add(path.rsplit("/", 1)[-1])
        desired = {group_map[g] for g in kc_groups if g in group_map}
        managed = set(group_map.values())
        current = set(
            user.groups.filter(name__in=managed).values_list("name", flat=True)
        )
        for name in desired - current:
            group, _ = Group.objects.get_or_create(name=name)
            user.groups.add(group)
        to_remove = current - desired
        if to_remove:
            user.groups.remove(*Group.objects.filter(name__in=to_remove))
        # Membership in the ``admin`` Django group implies staff+superuser so Jazzmin admin is
        # reachable without a separate provisioning step. The flags and the group are managed
        # together: gaining the group grants them, losing it revokes them.
        #
        # The revoke half is not optional. Without it, promotion was a one-way door: removing
        # somebody from the Keycloak admins group dropped their ``admin`` group here but left
        # is_superuser set forever, and is_superuser bypasses every permission check in the app.
        # So deprovisioning an administrator in the identity provider did not actually take away
        # their powers, which is the whole point of deprovisioning.
        #
        # Keyed on the managed group *moving* (``admin`` in ``to_remove``), not merely on ``admin``
        # being absent from this login. A superuser provisioned by hand - ``createsuperuser``, never
        # a member of the ``admin`` group - must not be demoted the first time they happen to sign in
        # through OIDC without that group. Promotion is what puts an account in the group, so an
        # account this mechanism promoted is the only kind it will demote.
        if "admin" in desired:
            if not user.is_superuser:
                user.is_staff = True
                user.is_superuser = True
                user.save(update_fields=["is_staff", "is_superuser"])
        elif "admin" in to_remove and (user.is_staff or user.is_superuser):
            user.is_staff = False
            user.is_superuser = False
            user.save(update_fields=["is_staff", "is_superuser"])

    @override
    def create_user(self, claims):
        user = super().create_user(claims)
        self._sync_groups(user, claims)
        return user

    def _adopt_username(self, user, claims: dict[str, Any]) -> None:
        """Move an existing account onto its Keycloak username.

        Without this, only accounts created after the change get a readable name and everyone who
        had already signed in keeps their hash for good, because ``get_username`` is consulted on
        creation and never again. Safe to do, and this is the reason it is safe: mozilla-django-oidc
        matches users by **email** (``filter_users_by_claims``), so the username is a label and
        nothing resolves through it.

        Left alone when somebody else already holds the name. A rename is cosmetic and losing a
        login to an IntegrityError over it would not be.
        """
        wanted = username_from_claims(claims.get("email"), claims)
        if user.username == wanted:
            return
        taken = (
            type(user)
            ._default_manager.filter(username=wanted)
            .exclude(pk=user.pk)
            .exists()
        )
        if taken:
            return
        user.username = wanted
        user.save(update_fields=["username"])

    @override
    def update_user(self, user, claims):
        user = super().update_user(user, claims)
        self._adopt_username(user, claims)
        self._sync_groups(user, claims)
        return user


class ApiTokenAuthentication(authentication.BaseAuthentication):
    keyword = "Bearer"

    @override
    def authenticate(self, request: Request) -> tuple[Any, ApiToken] | None:
        auth = authentication.get_authorization_header(request).split()
        if not auth or auth[0].lower() != self.keyword.lower().encode():
            return None
        if len(auth) != 2:
            raise exceptions.AuthenticationFailed("Invalid bearer token header.")
        raw = auth[1].decode("utf-8")
        token = ApiToken.resolve(raw)
        if token is None:
            raise exceptions.AuthenticationFailed("Invalid or expired token.")
        # Use update() to skip auto_now fields and avoid racing with concurrent
        # requests using the same token.
        ApiToken.objects.filter(pk=token.pk).update(last_used_at=timezone.now())
        return (token.user, token)

    @override
    def authenticate_header(self, request: Request) -> str:
        return self.keyword
