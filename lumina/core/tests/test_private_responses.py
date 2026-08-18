"""A signed-in page is not the browser's to reuse without asking.

Reported as a review queue badge reading zero with two runs waiting, which a hard refresh
fixed - and a hard refresh is precisely what bypasses the browser cache, so the 30-second
server-side count cache was not the culprit. Measured: authenticated responses carried
``Vary: Cookie`` and no ``Cache-Control`` at all, which leaves the browser free to reuse a page
it was told nothing about.

The staleness is the visible half. The half worth more is that after signing out on a shared
machine, Back could redisplay the dashboard, a run, or the queue.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import Group, User
from django.urls import reverse

pytestmark = pytest.mark.django_db

DIRECTIVE = "private, no-cache, must-revalidate"


@pytest.fixture
def reviewer(client):
    user = User.objects.create_user("cache-rev", password="pw")
    user.groups.add(Group.objects.get_or_create(name="reviewer")[0])
    client.force_login(user)
    return user


def test_a_signed_in_page_must_be_revalidated_before_it_is_reused(client, reviewer):
    response = client.get(reverse("accounts:dashboard"))

    assert response.headers["Cache-Control"] == DIRECTIVE


def test_the_review_queue_too(client, reviewer):
    """The page the report came from."""
    response = client.get(reverse("review:queue"))

    assert response.headers["Cache-Control"] == DIRECTIVE


def test_it_revalidates_rather_than_refusing_to_store(client, reviewer):
    """``no-store`` would also fix this and would disable the back/forward cache with it, so
    Back after a mis-click loses a half-typed submission form. These pages hold work in
    progress, not secrets that must never touch a disk."""
    response = client.get(reverse("accounts:dashboard"))

    assert "no-store" not in response.headers["Cache-Control"]
    assert "no-cache" in response.headers["Cache-Control"]


def test_an_anonymous_page_is_left_cacheable(client):
    """The public catalog is meant to be cached, and the SEO work depends on it."""
    response = client.get(reverse("hardware:systems"))

    assert "Cache-Control" not in response.headers


def test_a_view_that_has_said_something_specific_keeps_it(client, reviewer):
    """``robots.txt`` asks for a day of caching on purpose. A blanket header applied over the
    top would quietly undo every deliberate one."""
    response = client.get(reverse("core:robots"))

    assert "max-age=86400" in response.headers["Cache-Control"]
    assert DIRECTIVE not in response.headers["Cache-Control"]


def test_signing_out_is_what_this_protects(client, reviewer):
    """The sequence the header exists for: the browser holding a dashboard, the session ending,
    and the next look at that URL having to ask rather than answer from what it kept."""
    dashboard = client.get(reverse("accounts:dashboard"))
    assert dashboard.status_code == 200
    client.logout()

    after = client.get(reverse("accounts:dashboard"))

    assert after.status_code in (302, 403), "the revalidation must not return the page"
