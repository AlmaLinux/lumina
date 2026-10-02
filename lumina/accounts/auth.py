"""Authentication backends.

``LuminaOIDCBackend`` extends mozilla-django-oidc to sync Keycloak group
membership onto the Django user's groups on each login, using the map in
``settings.LUMINA_OIDC_GROUP_MAP``. Only groups that appear in the map are
touched - unrelated Django group memberships are preserved. ``claimed_group_keys``
is the matching rule: which spellings of a Keycloak group path that map may be
keyed on, including the ancestors of a nested group.

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


def claimed_username(claims: dict[str, Any] | None) -> str:
    """The Keycloak username these claims carry, or "" if they carry none usable.

    Tried in the order ``settings.LUMINA_OIDC_USERNAME_CLAIMS`` lists, first usable one winning,
    because which claim publishes the username is a property of the realm rather than of this
    application.

    Returns "" rather than a fallback on purpose: the caller that *names* an account can settle for
    a hash, but the caller that *matches* one must be able to tell "the realm said harriebird" from
    "the realm said nothing", and a fallback would make a derived string look like a claim.

    Nothing here may raise. It runs inside the login, and a malformed claim should cost a readable
    username, not the session.
    """
    for name in settings.LUMINA_OIDC_USERNAME_CLAIMS:
        candidate = ((claims or {}).get(name) or "")
        if not isinstance(candidate, str):
            continue
        candidate = candidate.strip()
        if not candidate or len(candidate) > _USERNAME_MAX:
            continue
        try:
            _USERNAME_VALIDATOR(candidate)
        except ValidationError:
            continue
        return candidate
    return ""


def username_from_claims(email: str | None, claims: dict[str, Any] | None = None) -> str:
    """The Django username for a Keycloak account: its own username.

    mozilla-django-oidc's default is a base64 SHA-224 of the email address, on the reasoning that
    usernames are often public identifiers and an email address should not be. That is sound for a
    provider that gives you nothing better, and wrong here: Keycloak publishes the account's own
    username, which is no more sensitive than the person's own login. The default put a
    38-character hash in the navigation bar where a name belongs, and it looked enough like an
    opaque database key to be reported as one.

    Falls back to the hash when the realm published nothing usable, so a bad claim costs a pretty
    username rather than the login.
    """
    return claimed_username(claims) or default_username_algo(email, claims)


def claimed_group_keys(
    raw_groups: Any, *, include_parents: bool = True
) -> set[str]:
    """Every spelling of the caller's Keycloak groups that the group map may be keyed on.

    Keycloak sends group *paths*, and what they look like depends on a checkbox in the mapper. With
    "Full group path" off it is "admins"; with it on, "/admins", and for a nested group
    "/lumina-admins/admins". Both the whole path and its last segment are matched, so the map works
    whichever way that checkbox is set and whether or not the group is nested.

    Matching the last segment means a group at any depth named "admins" maps to Django's "admin",
    which for a realm the deployment controls is the point: the alternative is a sign-in that
    succeeds and grants nothing, with no error anywhere to explain it. Read it as "group names are
    meaningful within the realm", and if that is not true of yours, key the map on full paths and
    set ``LUMINA_OIDC_GROUP_NESTED_PARENTS`` off - which also drops the ancestor walk below.

    With ``include_parents`` (the ``LUMINA_OIDC_GROUP_NESTED_PARENTS`` setting) the walk continues up
    the path, so "/lumina-admins/admins" yields "lumina-admins" as well. That is what lets a map
    keyed on the *parent* match a caller Keycloak only ever reports as a member of the child, which
    is how a FreeIPA nested group arrives: FreeIPA expresses "the admins are Lumina admins" by
    putting the ``admins`` group inside ``lumina-admins``, Keycloak's LDAP mapper imports that as a
    subgroup when "Preserve Group Inheritance" is on, and Keycloak does not propagate membership
    from a subgroup up to its parent. Each ancestor is matched by full path and by bare name, the
    same two ways the group itself is.
    """
    keys: set[str] = set()
    for raw in raw_groups or []:
        if not isinstance(raw, str):
            continue
        segments = [seg for seg in raw.strip().strip("/").split("/") if seg]
        if not segments:
            continue
        depths = range(len(segments), 0, -1) if include_parents else [len(segments)]
        for depth in depths:
            keys.add("/".join(segments[:depth]))
            keys.add(segments[depth - 1])
    return keys


class LuminaOIDCBackend(OIDCAuthenticationBackend):
    """OIDC backend that syncs Keycloak groups into Django groups."""

    def _sync_groups(self, user, claims: dict[str, Any]) -> None:
        group_map: dict[str, str] = settings.LUMINA_OIDC_GROUP_MAP
        if not group_map:
            return
        kc_groups = claimed_group_keys(
            claims.get("groups"),
            include_parents=settings.LUMINA_OIDC_GROUP_NESTED_PARENTS,
        )
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
    def filter_users_by_claims(self, claims):
        """Find the account these claims belong to, by username before email.

        The library matches on email alone. That broke a login outright: a person whose Keycloak
        email had changed since their account was made matched nothing, so the backend went on to
        *create* an account - with the username they already had. ``username`` is unique, so the
        insert raised ``IntegrityError`` and returned a 500, identically on every retry. There is
        no self-service way out of that; the account is simply locked out.

        The realm guarantees usernames are unique and never change, which makes the username the
        stable identifier here and the email the mutable attribute - the opposite of the order the
        library assumes. So the username is tried first, and ``update_user`` carries the new email
        onto the account rather than treating it as a different person.

        Email remains the fallback, and is not vestigial: accounts created before Keycloak's
        username was adopted still carry the hash, so their rows cannot be found by username at
        all. They match by email, and ``_adopt_username`` then moves them onto their real name,
        after which they match by username like everybody else.
        """
        username = claimed_username(claims)
        if username:
            by_username = self.UserModel.objects.filter(username=username)
            if by_username.exists():
                return by_username
        return super().filter_users_by_claims(claims)

    @override
    def create_user(self, claims):
        user = super().create_user(claims)
        self._sync_groups(user, claims)
        return user

    def _adopt_email(self, user, claims: dict[str, Any]) -> None:
        """Keep the account's email in step with the realm's.

        The library's ``update_user`` returns the user untouched, which was harmless while email
        was the thing accounts were found by - a changed address simply made a new account. Now
        that the username is what matches, a stale address would persist forever on an account
        that keeps signing in happily, and every notification this platform sends would go to an
        address its owner has already abandoned.
        """
        email = (claims.get("email") or "").strip()
        if email and email != user.email:
            user.email = email
            user.save(update_fields=["email"])

    def _adopt_username(self, user, claims: dict[str, Any]) -> None:
        """Move an existing account onto its Keycloak username.

        Without this, only accounts created after the change get a readable name and everyone who
        had already signed in keeps their hash for good, because ``get_username`` is consulted on
        creation and never again.

        This is also how a legacy account stops being legacy. ``filter_users_by_claims`` matches on
        username first and falls back to email; a hash-named account can only be found by the
        fallback, and renaming it here is what moves it onto the primary match for every login
        after this one.

        Left alone when somebody else already holds the name. The rename is not worth a failed
        login, and under username matching a collision here would mean the realm has handed one
        name to two accounts - which is the realm's to resolve, not ours to paper over by
        reassigning somebody's identity.
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
        self._adopt_email(user, claims)
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
