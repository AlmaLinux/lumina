"""Tests for LuminaOIDCBackend group sync.

Behavior pinned down:

- Keycloak claim ``groups`` maps to Django groups via
  ``settings.LUMINA_OIDC_GROUP_MAP``.
- Unmapped Keycloak groups are ignored.
- Group names from Keycloak may be prefixed with ``/``, and nested groups arrive as
  ``/parent/child``; both the whole path and its last segment are matched, so the map works
  whichever way the mapper's "Full group path" checkbox is set.
- A nested group also matches a map keyed on any of its **ancestors**, which is what makes a
  FreeIPA group nested into ``lumina-admins`` grant what ``lumina-admins`` maps to. Keycloak does
  not propagate membership from a subgroup up to its parent, so without this the parent is never
  named in the claim at all. ``LUMINA_OIDC_GROUP_NESTED_PARENTS`` turns it off.
- **The shipped map lets the AlmaLinux realm's ``admins`` group in with no configuration.** An empty
  or wrong map is invisible: sign-in succeeds, the user has no permissions, and nothing reports an
  error, so this is pinned against the real default rather than a fixture.
- Groups managed by the map are added/removed to match the claim on each
  login; unrelated Django group memberships are left alone.
- Membership in the Django ``admin`` group implies ``is_staff`` and
  ``is_superuser`` (so Jazzmin admin works).
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group

from lumina.accounts.auth import LuminaOIDCBackend, claimed_group_keys

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture(autouse=True)
def _group_map(settings):
    settings.LUMINA_OIDC_GROUP_MAP = {
        "almalinux-admins": "admin",
        "almalinux-reviewers": "reviewer",
    }


@pytest.fixture
def user():
    return User.objects.create_user(username="carol")


class GroupSyncTests:
    def _sync(self, user, groups):
        backend = LuminaOIDCBackend.__new__(LuminaOIDCBackend)  # skip __init__ net calls
        backend._sync_groups(user, {"groups": groups})

    def test_mapped_group_is_added(self, user):
        self._sync(user, ["almalinux-reviewers"])
        assert user.groups.filter(name="reviewer").exists()

    def test_unmapped_group_is_ignored(self, user):
        self._sync(user, ["some-other-group"])
        assert user.groups.count() == 0

    def test_leading_slash_stripped(self, user):
        self._sync(user, ["/almalinux-reviewers"])
        assert user.groups.filter(name="reviewer").exists()

    def test_removes_group_no_longer_claimed(self, user):
        reviewer = Group.objects.create(name="reviewer")
        user.groups.add(reviewer)
        self._sync(user, [])
        assert not user.groups.filter(name="reviewer").exists()

    def test_does_not_touch_unmanaged_groups(self, user):
        unrelated = Group.objects.create(name="unrelated-app-group")
        user.groups.add(unrelated)
        self._sync(user, ["almalinux-reviewers"])
        assert user.groups.filter(name="unrelated-app-group").exists()

    def test_admin_group_promotes_to_superuser(self, user):
        assert not user.is_staff
        self._sync(user, ["almalinux-admins"])
        user.refresh_from_db()
        assert user.is_staff
        assert user.is_superuser

    def test_non_admin_does_not_promote(self, user):
        self._sync(user, ["almalinux-reviewers"])
        user.refresh_from_db()
        assert not user.is_staff
        assert not user.is_superuser

    def test_losing_the_admin_group_revokes_superuser(self, user):
        """Deprovisioning has to actually deprovision.

        This is the security fix: promotion used to be a one-way door, so removing an
        administrator from the Keycloak admins group dropped the Django group but left
        is_superuser set forever. Since is_superuser bypasses every permission check, the account
        kept full admin after being offboarded.
        """
        self._sync(user, ["almalinux-admins"])
        user.refresh_from_db()
        assert user.is_superuser and user.is_staff, "premise: they were promoted"

        self._sync(user, [])
        user.refresh_from_db()

        assert not user.groups.filter(name="admin").exists()
        assert not user.is_superuser
        assert not user.is_staff

    def test_a_hand_provisioned_superuser_is_not_demoted(self, user):
        """A local superuser who never went through this mechanism must survive an OIDC login.

        ``createsuperuser`` sets is_superuser without adding the ``admin`` group, so this login,
        carrying no admin group, must not read as "lost the admin group" and strip their flags.
        The demotion is keyed on the managed group moving, not on admin being absent, precisely so
        this account is left alone.
        """
        user.is_staff = True
        user.is_superuser = True
        user.save(update_fields=["is_staff", "is_superuser"])

        self._sync(user, ["almalinux-reviewers"])
        user.refresh_from_db()

        assert user.is_superuser
        assert user.is_staff

    def test_losing_the_admin_group_with_flags_already_clear_writes_nothing(self, user):
        """The demotion branch must not save when there is nothing to clear.

        The case that exercises the guard: an account that holds the ``admin`` Django group but
        whose flags are already false (added to the group by hand without the flags, or the flags
        cleared out of band), losing the group this login. There is nothing to write, so it must
        not issue a user-row UPDATE. Mirrors the promotion branch's own ``and not user.is_superuser``
        guard, and rules out a demotion branch that saves on membership change alone.
        """
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        user.groups.add(Group.objects.get_or_create(name="admin")[0])
        assert not user.is_superuser and not user.is_staff, "premise: flags already clear"

        with CaptureQueriesContext(connection) as ctx:
            self._sync(user, [])          # loses the admin group

        assert not user.groups.filter(name="admin").exists()
        user_updates = [
            q["sql"] for q in ctx.captured_queries
            if "UPDATE" in q["sql"].upper() and "AUTH_USER" in q["sql"].upper()
            and "GROUP" not in q["sql"].upper()
        ]
        assert user_updates == [], user_updates


# --- what a fresh install grants, with nothing configured ----------------------
#
# Deliberately outside GroupSyncTests and its fixture: these read the map the application ships,
# not one a test set up, because "works at install time" is the requirement.


def _sync(user, groups):
    backend = LuminaOIDCBackend.__new__(LuminaOIDCBackend)  # skip __init__ net calls
    backend._sync_groups(user, {"groups": groups})


def test_the_shipped_map_covers_the_realms_admins_group():
    from lumina.settings.base import LUMINA_OIDC_GROUP_MAP

    assert LUMINA_OIDC_GROUP_MAP["admins"] == "admin"


@pytest.mark.parametrize("claimed", [
    pytest.param("admins", id="plain"),
    pytest.param("/admins", id="leading-slash"),
    pytest.param("/almalinux/admins", id="nested-full-path"),
])
def test_an_admins_member_gets_superuser_out_of_the_box(claimed, settings, user):
    """Every spelling Keycloak can send for that group, against the shipped map.

    The three come from one checkbox in the group mapper, which is not something Lumina can see or
    control, so all three have to work or the deployment is back to configuring something.
    """
    from lumina.settings.base import LUMINA_OIDC_GROUP_MAP

    settings.LUMINA_OIDC_GROUP_MAP = LUMINA_OIDC_GROUP_MAP
    _sync(user, [claimed])
    user.refresh_from_db()

    assert user.groups.filter(name="admin").exists()
    assert user.is_staff
    assert user.is_superuser


def test_both_spellings_of_the_admin_group_reach_the_same_django_group(settings, user):
    """``admins`` and ``almalinux-admins`` are two names for one thing, so a user in both should end
    up in ``admin`` once rather than tripping over a duplicate."""
    from lumina.settings.base import LUMINA_OIDC_GROUP_MAP

    settings.LUMINA_OIDC_GROUP_MAP = LUMINA_OIDC_GROUP_MAP
    _sync(user, ["admins", "almalinux-admins"])

    assert user.groups.filter(name="admin").count() == 1


def test_an_unrelated_group_still_grants_nothing(settings, user):
    """The last-segment match must not turn every group into a permission."""
    from lumina.settings.base import LUMINA_OIDC_GROUP_MAP

    settings.LUMINA_OIDC_GROUP_MAP = LUMINA_OIDC_GROUP_MAP
    _sync(user, ["/almalinux/mirror-operators", "packagers"])
    user.refresh_from_db()

    assert user.groups.count() == 0
    assert not user.is_staff
    assert not user.is_superuser


# --- what we ask Keycloak for ---------------------------------------------------


def test_the_default_scopes_do_not_ask_for_a_groups_scope():
    """Reported from the dev site: Keycloak answered the authorization request with

        error=invalid_scope&error_description=Invalid+scopes:+openid+email+profile+groups

    Keycloak validates every requested scope against the client's assigned client scopes and
    rejects the whole request if one is unknown, so asking for a scope the realm has not been given
    is not a degraded sign-in, it is no sign-in at all. There is no built-in client scope named
    "groups"; one has to be created by hand.

    And it buys nothing. Verified against Keycloak 26: with the Group Membership mapper on the
    client's dedicated scope, the "groups" claim is in both the access and ID tokens for a request
    asking only for "openid". So the default has to be the request that works against a realm
    nobody has prepared specially, and a realm that does have such a scope opts in through
    OIDC_RP_SCOPES.
    """
    from lumina.settings.base import OIDC_RP_SCOPES

    assert "groups" not in OIDC_RP_SCOPES.split()
    # openid is not optional, and the group sync needs an identity to attach to.
    assert "openid" in OIDC_RP_SCOPES.split()
    assert "email" in OIDC_RP_SCOPES.split()


def test_group_sync_does_not_depend_on_the_scope_being_requested(settings, user):
    """The claim is what matters, not how it was asked for. Pinned because the obvious repair for
    the error above would have been to keep the scope and make the realm match it."""
    from lumina.settings.base import LUMINA_OIDC_GROUP_MAP

    settings.LUMINA_OIDC_GROUP_MAP = LUMINA_OIDC_GROUP_MAP
    settings.OIDC_RP_SCOPES = "openid email profile"
    _sync(user, ["admins"])
    user.refresh_from_db()

    assert user.is_superuser


# --- the username a Keycloak account gets -----------------------------------------


def test_the_username_is_the_keycloak_username():
    """Reported from the dev site: "instead of using my account's username it's a big hashed string
    which appears to be a database ID of some sort".

    It was not an ID. mozilla-django-oidc's default ``get_username`` is a base64 SHA-224 of the email
    address, on the reasoning that usernames are often public identifiers and an email should not be.
    Keycloak gives us something better: ``preferred_username`` is the account's own name.
    """
    from lumina.accounts.auth import username_from_claims

    claims = {"email": "jwright@cloudlinux.com", "preferred_username": "jwright"}

    assert username_from_claims(claims["email"], claims) == "jwright"


def test_the_backend_actually_uses_it():
    """The link that makes the function above matter.

    Deliberately no settings override: this goes through mozilla-django-oidc's own ``get_username``
    against the settings the application ships, so it fails if ``OIDC_USERNAME_ALGO`` is not pointed
    at our function. Without this test, deleting that one line from base.py left every other test
    here green while every real login went back to a hash.
    """
    backend = LuminaOIDCBackend.__new__(LuminaOIDCBackend)
    claims = {"email": "jwright@cloudlinux.com", "preferred_username": "jwright"}

    assert backend.get_username(claims) == "jwright"


def test_a_new_account_is_created_with_that_name(db):
    """And through ``create_user``, which is the path a first-time sign-in takes."""
    backend = LuminaOIDCBackend.__new__(LuminaOIDCBackend)
    backend.UserModel = User

    created = backend.create_user({"email": "newperson@example.org",
                                   "preferred_username": "newperson"})

    assert created.username == "newperson"
    assert created.email == "newperson@example.org"


@pytest.mark.parametrize("claims,why", [
    pytest.param({}, "no claim at all", id="absent"),
    pytest.param({"preferred_username": ""}, "empty", id="empty"),
    pytest.param({"preferred_username": "   "}, "whitespace only", id="blank"),
    pytest.param({"preferred_username": "has spaces"}, "rejected by the username validator",
                 id="invalid-characters"),
    pytest.param({"preferred_username": "x" * 151}, "longer than the field", id="too-long"),
])
def test_an_unusable_claim_falls_back_to_the_hash(claims, why):
    """Never raises and never invents. This runs inside the login, so a claim we cannot use should
    cost a pretty username, not the session."""
    from mozilla_django_oidc.auth import default_username_algo

    from lumina.accounts.auth import username_from_claims

    email = "jwright@cloudlinux.com"
    full = dict(claims, email=email)

    assert username_from_claims(email, full) == default_username_algo(email, full), why


def test_an_account_created_before_this_gets_its_name_on_next_login(settings):
    """The repair half. ``get_username`` is consulted when the user is created and never again, so
    without this everybody who had already signed in keeps their hash for good.

    Safe because mozilla-django-oidc matches users by **email**, not by username, so the username is
    a label and nothing resolves through it.
    """
    from lumina.settings.base import LUMINA_OIDC_GROUP_MAP

    settings.LUMINA_OIDC_GROUP_MAP = LUMINA_OIDC_GROUP_MAP
    hashed = User.objects.create_user(username="bXxZnD3rKl4TGDk-kYTWENYZY9k",
                                      email="jwright@cloudlinux.com")
    backend = LuminaOIDCBackend.__new__(LuminaOIDCBackend)
    backend.update_user(hashed, {"email": "jwright@cloudlinux.com",
                                 "preferred_username": "jwright", "groups": ["admins"]})
    hashed.refresh_from_db()

    assert hashed.username == "jwright"
    # And the rest of the login still happened.
    assert hashed.is_superuser


def test_the_rename_gives_way_to_whoever_already_holds_the_name(settings):
    """A cosmetic rename must not cost somebody their login with an IntegrityError."""
    settings.LUMINA_OIDC_GROUP_MAP = {}
    User.objects.create_user(username="jwright", email="someone.else@example.org")
    hashed = User.objects.create_user(username="bXxZnD3rKl4TGDk-kYTWENYZY9k",
                                      email="jwright@cloudlinux.com")
    backend = LuminaOIDCBackend.__new__(LuminaOIDCBackend)
    backend.update_user(hashed, {"email": "jwright@cloudlinux.com",
                                 "preferred_username": "jwright"})
    hashed.refresh_from_db()

    assert hashed.username == "bXxZnD3rKl4TGDk-kYTWENYZY9k"


def test_a_settled_username_is_not_rewritten_on_every_login(settings, django_assert_num_queries):
    """No write when there is nothing to change, so a login is not a database update."""
    settings.LUMINA_OIDC_GROUP_MAP = {}
    user = User.objects.create_user(username="jwright", email="jwright@cloudlinux.com")
    backend = LuminaOIDCBackend.__new__(LuminaOIDCBackend)

    with django_assert_num_queries(0):
        backend.update_user(user, {"email": "jwright@cloudlinux.com",
                                   "preferred_username": "jwright"})


class NestedGroupTests:
    """A FreeIPA group nested inside another, arriving through Keycloak.

    FreeIPA says "the admins are Lumina admins" by putting the ``admins`` group *into*
    ``lumina-admins``. Keycloak's LDAP group mapper, with "Preserve Group Inheritance" on, imports
    that as a subgroup, so the groups claim for somebody whose only direct membership is ``admins``
    reads ``["/lumina-admins/admins"]`` - it names the child and never mentions the parent, because
    Keycloak's own model does not propagate membership from a subgroup up to its parent (a subgroup
    inherits the parent's *roles*, not its membership). A map keyed on ``lumina-admins`` therefore
    matched nothing and the sign-in granted nothing, with no error anywhere to say so.
    """

    def _sync(self, user, groups):
        backend = LuminaOIDCBackend.__new__(LuminaOIDCBackend)
        backend._sync_groups(user, {"groups": groups})

    @pytest.fixture(autouse=True)
    def _nested_map(self, settings):
        settings.LUMINA_OIDC_GROUP_MAP = {"lumina-admins": "admin"}

    def test_membership_of_a_child_grants_the_parents_mapping(self, user):
        self._sync(user, ["/lumina-admins/admins"])
        assert user.groups.filter(name="admin").exists()
        assert user.is_superuser

    def test_the_walk_goes_all_the_way_up(self, user):
        """Nothing says the nesting is only one deep."""
        self._sync(user, ["/lumina-admins/sysadmins/admins"])
        assert user.groups.filter(name="admin").exists()

    def test_a_middle_ancestor_matches_by_bare_name(self, user, settings):
        """Ancestors are matched the same two ways the group itself is: full path and bare name."""
        settings.LUMINA_OIDC_GROUP_MAP = {"sysadmins": "admin"}
        self._sync(user, ["/almalinux/sysadmins/admins"])
        assert user.groups.filter(name="admin").exists()

    def test_an_ancestor_matches_by_full_path(self, user, settings):
        settings.LUMINA_OIDC_GROUP_MAP = {"almalinux/sysadmins": "admin"}
        self._sync(user, ["/almalinux/sysadmins/admins"])
        assert user.groups.filter(name="admin").exists()

    def test_a_sibling_subtree_still_grants_nothing(self, user):
        """The walk is upward only. Being in some other tree must not reach this mapping."""
        self._sync(user, ["/other-app/admins"])
        assert not user.groups.filter(name="admin").exists()
        assert not user.is_superuser

    def test_nesting_can_be_turned_off(self, user, settings):
        """For a realm whose subgroups narrow their parent rather than feed it, where treating a
        child as its parent would over-grant."""
        settings.LUMINA_OIDC_GROUP_NESTED_PARENTS = False
        self._sync(user, ["/lumina-admins/admins"])
        assert not user.groups.filter(name="admin").exists()

    def test_the_group_itself_still_matches_with_nesting_off(self, user, settings):
        settings.LUMINA_OIDC_GROUP_NESTED_PARENTS = False
        self._sync(user, ["/lumina-admins"])
        assert user.groups.filter(name="admin").exists()

    def test_a_parent_reported_directly_also_works(self, user):
        """The other Keycloak configuration for the same FreeIPA nesting: "Preserve Group
        Inheritance" off with the memberOf retrieval strategy, where FreeIPA's own memberof plugin
        has already flattened the nesting and the claim lists both groups side by side."""
        self._sync(user, ["admins", "lumina-admins"])
        assert user.groups.filter(name="admin").exists()


class ClaimedGroupKeyTests:
    """The matching rule on its own, without a database."""

    def test_a_flat_name_yields_itself(self):
        assert claimed_group_keys(["admins"]) == {"admins"}

    def test_a_nested_path_yields_every_ancestor_by_path_and_name(self):
        assert claimed_group_keys(["/a/b/c"]) == {"a", "a/b", "a/b/c", "b", "c"}

    def test_without_parents_only_the_group_itself(self):
        assert claimed_group_keys(["/a/b/c"], include_parents=False) == {"a/b/c", "c"}

    def test_junk_in_the_claim_does_not_raise(self):
        """This runs inside a login. A malformed claim should cost a mapping, not the session."""
        assert claimed_group_keys(["", "/", "//", None, 7, {"a": 1}]) == set()

    def test_a_missing_claim_is_empty(self):
        assert claimed_group_keys(None) == set()
