"""Signing in when there is no Keycloak.

Reported straight from the dev stack: "Where can reviewer login? It doesn't seem to work at
/admin/login." It does not, and the instruction was mine.

The devstack drops ``mozilla_django_oidc`` because compose.yaml has no Keycloak, and it used
to set ``LOGIN_URL = "/admin/login/"``. Django's admin login form authenticates the
credentials and then refuses any account without ``is_staff`` - "Please enter the correct
username and password for a staff account". The seeded superuser sails through, so the hole
stayed invisible: **every non-staff account was locked out of the dev stack**, including the
seeded ``reviewer`` and any submitter or vendor account created to exercise the submitter
flows. Which is most of what this application is for.

There was no sign-out control anywhere either, so switching between the two seeded accounts
meant clearing cookies.

Both routes take the URL names ``mozilla_django_oidc`` publishes
(``oidc_authentication_init``, ``oidc_logout``) so the base templates link to them with no
branch, and both are mounted **only** in the branch that runs when the OIDC app is absent.
Production must never grow a password form, and ``test_production_has_no_password_login``
is what keeps that true.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth import views as auth_views
from django.contrib.auth.models import Group, User
from django.test import override_settings
from django.urls import URLResolver, path, reverse

import lumina.urls

pytestmark = pytest.mark.django_db


# The devstack URL shape, built here because the test settings keep OIDC installed: strip
# the mozilla include and mount what the non-OIDC branch mounts. Kept in step with
# ``lumina/urls.py`` by ``test_the_devstack_branch_mounts_these_views`` below.
def _without_oidc_include(patterns):
    return [
        entry for entry in patterns
        if not (isinstance(entry, URLResolver) and str(entry.pattern) == "oidc/")
    ]


urlpatterns = [
    path(
        "oidc/authenticate/",
        auth_views.LoginView.as_view(template_name="core/devstack_login.html"),
        name="oidc_authentication_init",
    ),
    path("oidc/logout/", auth_views.LogoutView.as_view(), name="oidc_logout"),
    *_without_oidc_include(lumina.urls.urlpatterns),
]

devstack_urls = override_settings(ROOT_URLCONF=__name__)


@pytest.fixture
def reviewer():
    user = User.objects.create_user("reviewer", password="reviewer")
    group, _ = Group.objects.get_or_create(name="reviewer")
    user.groups.add(group)
    return user


# --- the bug -------------------------------------------------------------------


def test_the_admin_form_refuses_a_non_staff_account(client, reviewer):
    """Why /admin/login/ was the wrong place to send anybody.

    The password is correct and the form still says no, which is why this looked like a
    wrong password rather than a wrong URL.
    """
    from django.contrib.auth import authenticate

    assert authenticate(username="reviewer", password="reviewer") == reviewer

    client.post(
        "/admin/login/", {"username": "reviewer", "password": "reviewer"},
    )

    assert "_auth_user_id" not in client.session


def test_devstack_no_longer_points_login_at_the_admin():
    """Read off the settings module itself, since the dev stack is the only place this
    applies and the test suite runs with OIDC installed."""
    import lumina.settings.devstack as devstack

    assert devstack.LOGIN_URL != "/admin/login/"
    assert devstack.LOGIN_URL == "/oidc/authenticate/"


# --- signing in ----------------------------------------------------------------


@devstack_urls
def test_a_reviewer_can_sign_in(client, reviewer):
    """The reported case, end to end."""
    resp = client.post(
        reverse("oidc_authentication_init"),
        {"username": "reviewer", "password": "reviewer"},
    )

    assert resp.status_code == 302
    assert client.session.get("_auth_user_id") == str(reviewer.pk)


@devstack_urls
def test_signing_in_reaches_the_review_queue(client, reviewer):
    """Being able to log in is only useful if the session then works, and the reviewer's
    whole reason to log in is behind ``reviewer_required``."""
    client.post(
        reverse("oidc_authentication_init"),
        {"username": "reviewer", "password": "reviewer"},
    )

    assert client.get(reverse("review:queue")).status_code == 200


@devstack_urls
def test_a_plain_user_can_sign_in_too(client):
    """Not a reviewer privilege. A submitter account is the one most of the recent work
    needs, and it was locked out for exactly the same reason."""
    User.objects.create_user("submitter", password="pw")

    resp = client.post(
        reverse("oidc_authentication_init"),
        {"username": "submitter", "password": "pw"},
    )

    assert resp.status_code == 302
    assert "_auth_user_id" in client.session


@devstack_urls
def test_a_wrong_password_is_still_refused(client, reviewer):
    resp = client.post(
        reverse("oidc_authentication_init"),
        {"username": "reviewer", "password": "wrong"},
    )

    assert resp.status_code == 200
    assert "_auth_user_id" not in client.session


@devstack_urls
def test_the_form_says_it_is_a_development_login(client):
    """So nobody mistakes it for something the real deployment offers."""
    body = client.get(reverse("oidc_authentication_init")).content.decode()

    assert "Development sign-in" in body
    assert "Keycloak" in body


@devstack_urls
def test_the_form_lists_the_seeded_accounts(client):
    """Which accounts exist, because hunting through seed_devstack.py for them is friction
    with no upside."""
    body = " ".join(client.get(reverse("oidc_authentication_init")).content.decode().split())

    assert "admin" in body
    assert "reviewer" in body


@devstack_urls
def test_the_form_does_not_claim_a_password_it_cannot_know(client):
    """It used to say admin/admin and reviewer/reviewer, which are seed_devstack's fallbacks
    and true only under compose.yaml.

    A deployed dev host is reachable by anybody, so the Ansible play generates both passwords
    and passes them through DEVSTACK_ADMIN_PASSWORD / DEVSTACK_REVIEWER_PASSWORD. On that host
    the page was stating a password that had never been set, which reads as broken logins
    rather than as stale text, and cost somebody an afternoon.
    """
    body = " ".join(client.get(reverse("oidc_authentication_init")).content.decode().split())

    assert "admin</code> / <code>admin" not in body
    assert "reviewer</code> / <code>reviewer" not in body
    # Says where they actually are instead.
    assert "/etc/lumina-dev-accounts.env" in body


@devstack_urls
def test_login_honours_next(client, reviewer):
    """``@login_required`` sends people here with ?next=, and dropping it would land every
    redirect on the home page."""
    resp = client.post(
        f"{reverse('oidc_authentication_init')}?next=/review/",
        {"username": "reviewer", "password": "reviewer", "next": "/review/"},
    )

    assert resp["Location"] == "/review/"


# --- signing out ---------------------------------------------------------------


@devstack_urls
def test_sign_out_ends_the_session(client, reviewer):
    client.force_login(reviewer)

    client.post(reverse("oidc_logout"))

    assert "_auth_user_id" not in client.session


@devstack_urls
def test_the_nav_offers_sign_out_when_signed_in(client, reviewer):
    """There was no way out at all, which made switching between the seeded accounts a
    cookie-clearing exercise."""
    client.force_login(reviewer)

    body = client.get(reverse("accounts:dashboard")).content.decode()

    assert "Sign out" in body
    assert reverse("oidc_logout") in body


@devstack_urls
def test_sign_out_is_a_post(client, reviewer):
    """Django's LogoutView has refused GET since 4.1, so a plain link would 405. The nav
    uses a form."""
    client.force_login(reviewer)
    body = client.get(reverse("accounts:dashboard")).content.decode()

    marker = body.index(reverse("oidc_logout"))
    assert 'method="post"' in body[marker - 120:marker]


# --- production is untouched ---------------------------------------------------


# --- which world the devstack is in ---------------------------------------------
#
# The password form exists only when ``mozilla_django_oidc`` is absent, and whether it is absent
# is decided in ``lumina/settings/devstack.py`` by whether a realm has been configured. That makes
# these two the tests that keep a public dev host from carrying a password form once it has
# Keycloak, and from losing its only way in when it has not.


def _devstack_namespace(**environ):
    """Import lumina.settings.devstack in a fresh interpreter with a chosen environment, and hand
    back the three things these tests ask about.

    A subprocess, not exec of the module source: ``devstack.py`` reads the OIDC values through
    ``from lumina.settings.base import *``, and inside a running test ``lumina.settings.base`` is
    already imported and its env-derived values frozen. Exec'ing the source therefore re-read the
    cached module and reported "no realm" whatever the environment said, which passed the
    unconfigured test and failed the configured one for a reason that had nothing to do with the
    branch under test.
    """
    import json
    import os
    import subprocess
    import sys

    probe = (
        "import json;"
        "from lumina.settings import devstack as d;"
        "print(json.dumps({"
        "'_oidc_configured': d._oidc_configured,"
        "'INSTALLED_APPS': list(d.INSTALLED_APPS),"
        "'AUTHENTICATION_BACKENDS': list(d.AUTHENTICATION_BACKENDS)}))"
    )
    env = dict(os.environ, DJANGO_SETTINGS_MODULE="lumina.settings.devstack",
               SECRET_KEY="probe", DB_ENGINE="sqlite3", **environ)
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                         env=env, cwd=str(Path(lumina.urls.__file__).parent.parent))
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.splitlines()[-1])


def test_a_devstack_with_a_realm_uses_keycloak_and_drops_the_password_form():
    """The requirement behind tying the dev site to Keycloak: configuring a realm must remove the
    password form rather than leave a second way in beside it.

    No separate switch does that. ``lumina/urls.py`` mounts the form only when the OIDC app is
    absent, so keeping the app installed is what removes it.
    """
    ns = _devstack_namespace(
        OIDC_RP_CLIENT_ID="lumina-dev",
        OIDC_OP_AUTHORIZATION_ENDPOINT="https://kc.example/realms/x/protocol/openid-connect/auth",
    )

    assert ns["_oidc_configured"] is True
    assert "mozilla_django_oidc" in ns["INSTALLED_APPS"]
    assert "lumina.accounts.auth.LuminaOIDCBackend" in ns["AUTHENTICATION_BACKENDS"]


def test_a_devstack_with_no_realm_keeps_the_password_form():
    """And the other direction, which is not symmetry: a dev host with no identity provider and
    no password form is a host nobody can log into."""
    ns = _devstack_namespace(OIDC_RP_CLIENT_ID="", OIDC_OP_AUTHORIZATION_ENDPOINT="")

    assert ns["_oidc_configured"] is False
    assert "mozilla_django_oidc" not in ns["INSTALLED_APPS"]
    assert ns["AUTHENTICATION_BACKENDS"] == ["django.contrib.auth.backends.ModelBackend"]


def test_an_issuer_with_no_client_id_is_not_configured():
    """Half-configured counts as unconfigured. The Ansible env template used to write
    OIDC_OP_AUTHORIZATION_ENDPOINT from an empty issuer, giving the non-empty string
    "/protocol/openid-connect/auth", so a host with no realm looked configured and would have
    lost its password form in exchange for an OIDC app pointed at nothing.
    """
    ns = _devstack_namespace(
        OIDC_RP_CLIENT_ID="", OIDC_OP_AUTHORIZATION_ENDPOINT="/protocol/openid-connect/auth")

    assert ns["_oidc_configured"] is False
    assert "mozilla_django_oidc" not in ns["INSTALLED_APPS"]


def test_production_has_no_password_login():
    """The one that matters. A password form in the real deployment would be a second way
    in, outside Keycloak, bypassing whatever it enforces.

    Asserted against the running URLconf, which under the test settings has OIDC installed
    and is therefore the production shape.
    """
    from django.urls import get_resolver

    assert "mozilla_django_oidc" in settings.INSTALLED_APPS, (
        "this test only means something with OIDC installed"
    )
    match = get_resolver().resolve("/oidc/authenticate/")
    view = match.func

    assert getattr(view, "view_class", None) is not auth_views.LoginView, (
        "production is serving Django's password LoginView"
    )


def test_the_devstack_branch_mounts_these_views():
    """Source-level, because the branch does not run under the test settings.

    Keeps the URLconf built at the top of this file honest: if the real branch stops
    mounting these, every test above still passes against a shape that no longer exists.
    """
    source = (Path(settings.BASE_DIR) / "lumina" / "urls.py").read_text()
    branch = source[source.index("if _oidc_installed:"):source.index("urlpatterns = [")]

    assert "LoginView" in branch
    assert "LogoutView" in branch
    assert 'name="oidc_authentication_init"' in branch
    assert 'name="oidc_logout"' in branch
    assert "core/devstack_login.html" in branch


def test_the_old_stub_is_gone():
    """It redirected to /admin/login/, which is the bug. A leftover would be a second,
    silently broken path to signing in."""
    source = (Path(settings.BASE_DIR) / "lumina" / "core" / "views.py").read_text()

    assert "oidc_login_stub" not in source
    assert "/admin/login/" not in source
