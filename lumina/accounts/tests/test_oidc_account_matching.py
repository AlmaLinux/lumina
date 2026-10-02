"""Which Django account a set of Keycloak claims belongs to.

Reported as a 500 on ``/oidc/callback/``:
``IntegrityError: (1062, "Duplicate entry 'harriebird' for key 'username'")``, nine times in four
minutes, followed by sixty-eight "state not found" errors as the person reloaded a dead callback.

mozilla-django-oidc matches accounts on **email alone**, while the username comes from the realm
and is ``unique``. Somebody whose Keycloak email had changed since their account was made
therefore matched nothing, so the backend went on to create an account - with the username they
already had - and the insert collided. Identically on every retry, with no way out from the
person's side.

The realm guarantees usernames are unique and never change, which makes the username the stable
identifier and the email the mutable attribute: the opposite of the order the library assumes.
So matching goes username first, email second, and a changed address is carried onto the account
rather than being read as a different person.

The collision is not caught and rethrown anywhere - it is designed out. If the account is found,
nothing tries to create it.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User

from lumina.accounts.auth import LuminaOIDCBackend, claimed_username

pytestmark = pytest.mark.django_db


def backend() -> LuminaOIDCBackend:
    instance = LuminaOIDCBackend.__new__(LuminaOIDCBackend)
    instance.UserModel = User
    return instance


def claims(**over) -> dict:
    base = {"email": "birdharrie@gmail.com", "preferred_username": "harriebird"}
    base.update(over)
    return base


# --- the reported lockout ----------------------------------------------------------


def test_a_changed_email_still_finds_the_account():
    """The report. The account exists under the address it was made with; the realm now sends a
    different one. Matching on email alone finds nothing and the login is lost."""
    user = User.objects.create_user("harriebird", email="harrie@oldhost.example")

    found = backend().filter_users_by_claims(claims())

    assert list(found) == [user]


def test_the_login_does_not_try_to_create_a_second_account():
    """What actually raised: ``get_or_create_user`` creates when the filter returns nothing, and
    the username it creates with is already taken. Asserted through the library's own entry point
    so the fix is proved where the failure happened, not one layer in."""
    user = User.objects.create_user("harriebird", email="harrie@oldhost.example")
    instance = backend()
    instance.get_userinfo = lambda *a, **kw: claims()

    returned = instance.get_or_create_user("access", "id", {})

    assert returned == user
    assert User.objects.filter(username="harriebird").count() == 1


def test_an_account_with_no_email_at_all_is_found():
    """The other way the rows drift apart: created while the realm sent no email claim, so the
    column is blank and an email match can never succeed."""
    user = User.objects.create_user("harriebird", email="")

    assert list(backend().filter_users_by_claims(claims())) == [user]


# --- the email is an attribute now, so it has to be kept ---------------------------


def test_the_new_address_is_written_to_the_account():
    """``update_user`` in the library returns the user untouched, which was harmless while email
    was what accounts were found by. Now it is not: a stale address would persist forever on an
    account signing in happily, and every notification would go somewhere its owner abandoned."""
    user = User.objects.create_user("harriebird", email="harrie@oldhost.example")

    backend().update_user(user, claims())

    user.refresh_from_db()
    assert user.email == "birdharrie@gmail.com"


def test_a_blank_email_claim_does_not_erase_the_address():
    """A realm that stops sending the claim is saying nothing, not saying "none"."""
    user = User.objects.create_user("harriebird", email="harrie@oldhost.example")

    backend().update_user(user, {"preferred_username": "harriebird"})

    user.refresh_from_db()
    assert user.email == "harrie@oldhost.example"


# --- the fallback still carries the accounts that need it ---------------------------


def test_a_legacy_hash_named_account_is_still_found_by_email():
    """Accounts made before Keycloak's username was adopted carry the hash, so they cannot be
    found by username at all. Removing the email fallback would lock out every one of them -
    trading the reported bug for a much larger one."""
    user = User.objects.create_user("1a2b3c4dhashhash", email="birdharrie@gmail.com")

    assert list(backend().filter_users_by_claims(claims())) == [user]


def test_such_an_account_is_renamed_so_it_matches_by_username_next_time():
    """How an account stops being legacy. Otherwise the fallback carries it forever and the
    account stays vulnerable to exactly the email change that caused the report."""
    user = User.objects.create_user("1a2b3c4dhashhash", email="birdharrie@gmail.com")

    backend().update_user(user, claims())

    user.refresh_from_db()
    assert user.username == "harriebird"
    assert list(backend().filter_users_by_claims(claims())) == [user]


def test_username_wins_over_email_when_they_name_different_accounts():
    """Two rows, one person's claims. The username is the identifier the realm guarantees, so it
    decides; matching the other would hand this login somebody else's account."""
    by_name = User.objects.create_user("harriebird", email="harrie@oldhost.example")
    User.objects.create_user("someone-else", email="birdharrie@gmail.com")

    assert list(backend().filter_users_by_claims(claims())) == [by_name]


# --- resolving the claim -----------------------------------------------------------


def test_a_dedicated_username_claim_is_preferred(settings):
    """A realm publishing its own ``username`` mapper is naming the same thing through a claim it
    controls, which is the stronger guarantee of the two."""
    assert claimed_username({"username": "harriebird",
                             "preferred_username": "something-else"}) == "harriebird"


def test_the_standard_claim_backs_it_up():
    """A realm with no such mapper must not fall through to the hash: under username matching
    that means a new account for every existing person, which is worse than the reported bug."""
    assert claimed_username({"preferred_username": "harriebird"}) == "harriebird"


def test_the_claim_list_can_be_pinned(settings):
    """Which claim carries the username is a property of the realm, so it is configuration."""
    settings.LUMINA_OIDC_USERNAME_CLAIMS = ["username"]

    assert claimed_username({"preferred_username": "harriebird"}) == ""


@pytest.mark.parametrize("bad,why", [
    ({}, "no claim at all"),
    ({"preferred_username": "   "}, "whitespace only"),
    ({"preferred_username": "has spaces"}, "the username validator rejects it"),
    ({"preferred_username": "x" * 151}, "longer than the column"),
    ({"preferred_username": 12345}, "not a string"),
])
def test_an_unusable_claim_resolves_to_nothing(bad, why):
    """"" rather than a fallback, because the caller that *matches* an account has to tell "the
    realm said harriebird" from "the realm said nothing". A derived string that looked like a
    claim would match accounts by a value the realm never sent."""
    assert claimed_username(bad) == "", why


def test_an_unusable_claim_does_not_match_every_blank_username():
    """The consequence of the line above, stated as behaviour: an empty resolution must not fall
    through to ``filter(username="")`` and match whatever is there."""
    # ``create`` rather than ``create_user``, which refuses a blank name. The row is what
    # matters: a blank username is storable, so the query must not be able to select it.
    User.objects.create(username="", email="nobody@example.org")

    found = backend().filter_users_by_claims({"email": "birdharrie@gmail.com"})

    assert list(found) == []
