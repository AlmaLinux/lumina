"""Can a person actually get at the things that exist?

Three features in this project have been unreachable rather than broken: a statistics page
with no link to it from anywhere, a notification event no configured endpoint was
subscribed to, and a model field missing from its admin form so the feature it controlled
could not be switched on. Each time the code worked and the tests passed, because a test
that asserts behaviour does not assert reachability - it calls the function directly, or
posts to the URL by name, and never asks whether a human could have found either.

So this file only checks reachability, and deliberately makes weak assertions about
content: every registered admin exposes its whole model and renders, and every link in the
navigation goes somewhere. A strong assertion here would be a second copy of another
test's job and would break for unrelated reasons.
"""
from __future__ import annotations

import re

import pytest
from django.contrib import admin
from django.contrib.auth.models import Group, User
from django.test import RequestFactory
from django.urls import reverse

pytestmark = pytest.mark.django_db


# --- admin forms expose their whole model ----------------------------------------
#
# Fields deliberately kept off a form, with the reason. Anything else missing is the
# WebhookEndpoint.direct_messages bug again: a field that exists in the database and in
# the code, cannot be set by anybody, and leaves the feature it controls looking finished.
_INTENTIONALLY_ABSENT = {
    # Stamped from the request on save, never typed.
    "Component": {"created_by"},
    "Software": {"created_by"},
    # Django's own UserAdmin: the add page takes a username and password, and everything
    # else is on the change page's fieldsets. Not ours to restructure.
    "User": {
        "date_joined", "email", "first_name", "groups", "is_active", "is_staff",
        "is_superuser", "last_login", "last_name", "password", "user_permissions",
    },
}


def _superuser() -> User:
    return User.objects.create_superuser("reach-su", "su@example.com", "pw")


def _admin_request(user) -> object:
    request = RequestFactory().get("/admin/")
    request.user = user
    return request


def test_every_admin_form_exposes_every_editable_field():
    """A field on the model that no form renders cannot be set by anybody.

    This is the check that would have caught ``direct_messages``: the delivery code read
    it, the model had it, and the admin never showed it, so direct messages could not be
    turned on and nothing failed.
    """
    request = _admin_request(_superuser())
    gaps = {}
    for model, model_admin in admin.site._registry.items():
        editable = {
            field.name for field in model._meta.get_fields()
            if getattr(field, "editable", False) and not field.auto_created
        }
        form_fields = set(model_admin.get_form(request)().fields)
        readonly = set(model_admin.get_readonly_fields(request) or ())
        allowed = _INTENTIONALLY_ABSENT.get(model.__name__, set())
        missing = editable - form_fields - readonly - allowed
        if missing:
            gaps[model.__name__] = sorted(missing)

    assert not gaps, (
        "these model fields are on no admin form, so nobody can set them: "
        f"{gaps}. Add them to the admin, or to _INTENTIONALLY_ABSENT with the reason."
    )


def test_every_admin_changelist_and_add_page_renders(client):
    """A broken form or template shows up as a 500 here rather than when somebody needs it."""
    client.force_login(_superuser())
    broken = {}
    for model in admin.site._registry:
        meta = model._meta
        for view in ("changelist", "add"):
            url = reverse(f"admin:{meta.app_label}_{meta.model_name}_{view}")
            status = client.get(url).status_code
            # 403 on add is a deliberate "this is derived, do not create one by hand".
            if status not in (200, 403):
                broken[f"{model.__name__}:{view}"] = status
    assert not broken, broken


# --- navigation goes somewhere ---------------------------------------------------

_SKIP = re.compile(r"^(#|mailto:|https?://|/static/|/media/)")


def _internal_links(html: str) -> list[str]:
    """Every distinct in-project href on a page, in order."""
    seen, out = set(), []
    for href in re.findall(r'href="([^"]+)"', html):
        if _SKIP.match(href) or href in seen:
            continue
        seen.add(href)
        out.append(href)
    return out


def _check(client, links: list[str]) -> dict:
    """GET each link; report anything missing or exploding.

    A redirect is fine - a login gate or a canonical URL - and so is 405, which is a URL
    that exists but wants a POST. What must not appear is 404 (the link points nowhere)
    or 5xx (it points at something broken).
    """
    bad = {}
    for href in links:
        status = client.get(href).status_code
        if status == 404 or status >= 500:
            bad[href] = status
    return bad


def test_every_link_in_the_public_navigation_resolves(client):
    """The bug this catches: a page that exists, works, is tested, and has no link to it -
    or a link that was renamed on one side only."""
    home = client.get("/")
    links = _internal_links(home.content.decode())

    assert links, "the public pages have no links at all, which cannot be right"
    assert not _check(client, links)


def test_every_link_in_the_workspace_sidebar_resolves(client):
    """As a reviewer with staff access, so the Review and Admin sections render too and
    every one of their links is followed."""
    user = User.objects.create_user("reach-rev", password="pw", is_staff=True)
    user.groups.add(Group.objects.get_or_create(name="reviewer")[0])
    client.force_login(user)

    page = client.get(reverse("accounts:dashboard"))
    links = _internal_links(page.content.decode())

    assert reverse("review:queue") in links, "a reviewer must be offered the queue"
    assert not _check(client, links)


def test_the_survey_statistics_are_linked_from_the_public_navigation(client):
    """Named rather than left to the sweep above, because this is the page that went a
    week with nothing linking to it: the sweep only proves the links present are good, not
    that a particular page is among them."""
    home = client.get("/").content.decode()

    assert reverse("results:stats") in _internal_links(home)
