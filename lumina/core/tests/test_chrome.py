"""The furniture around the content: alerts, icons, menus, and the search box.

Six bugs found by inventorying the interface page by page, none of which any test noticed, and all
of which a reader meets rather than reads about. They have nothing in common except the thing that
made them invisible: each is a template detail that renders perfectly well as HTML and is simply
wrong on the screen. The strings were all present and correct.

Kept together because they are the same *kind* of check, and because a browser suite will
eventually assert the visual half of several of these. These are the parts that can be pinned
without one.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth.models import Group, User
from django.contrib.messages import constants as message_constants
from django.urls import reverse

from lumina.releases.models import AlmaLinuxRelease
from lumina.vendors.models import Vendor

pytestmark = pytest.mark.django_db

TEMPLATES = Path(settings.BASE_DIR) / "templates"


# --- alerts ------------------------------------------------------------------------


def test_an_error_message_gets_a_class_that_exists():
    """Django's tag for an error is the string "error", and both bases build their class as
    ``alert alert-{{ message.tags }}``. Neither Bootstrap nor Tabler defines ``.alert-error``: the
    class is ``.alert-danger``. So every refusal in the application rendered as unstyled body text
    with no red, no border, and no icon, and did it on both bases at once, so nothing looked out of
    place next to anything else."""
    assert settings.MESSAGE_TAGS[message_constants.ERROR] == "danger"
    assert settings.MESSAGE_TAGS[message_constants.DEBUG] == "secondary"


@pytest.mark.parametrize("level", ["debug", "info", "success", "warning", "error"])
def test_every_message_level_names_a_real_alert_class(level):
    """The general form of it. Bootstrap ships eight contextual alert classes; a level whose tag is
    not one of them is invisible whatever it says."""
    from django.contrib.messages import constants

    bootstrap = {"primary", "secondary", "success", "danger", "warning", "info", "light", "dark"}
    tag = settings.MESSAGE_TAGS.get(
        getattr(constants, level.upper()), constants.DEFAULT_TAGS[getattr(constants, level.upper())]
    )
    assert tag in bootstrap, f"messages.{level} renders as .alert-{tag}, which no stylesheet defines"


# --- icon fonts ---------------------------------------------------------------------
#
# The two layouts load different icon fonts: base_admin loads Bootstrap Icons, base_public loads
# Tabler. A ``bi-`` glyph on a public page is a blank space, and it is a blank space that looks
# exactly like intentional padding. Four of them shipped on the submitter's own listing form,
# beside the four lines that say whether a component will be created, matched, or skipped.


def _extends(name: str, seen: tuple = ()) -> set[str]:
    if name in seen:
        return set()
    match = re.search(r'{%\s*extends\s+"([^"]+)"', (TEMPLATES / name).read_text())
    if not match:
        return set()
    parent = match.group(1)
    return {parent} if parent.startswith("base_") else _extends(parent, seen + (name,))


def _includers() -> dict:
    out: dict = {}
    for path in TEMPLATES.rglob("*.html"):
        name = str(path.relative_to(TEMPLATES))
        for included in re.findall(r'{%\s*include\s+"([^"]+)"', path.read_text()):
            out.setdefault(included, set()).add(name)
    return out


def _bases_reaching(name: str, includers: dict, seen: tuple = ()) -> set[str]:
    """Every base layout a template can be rendered under, following includes upward."""
    if name in seen:
        return set()
    bases = set(_extends(name))
    for parent in includers.get(name, ()):
        bases |= _bases_reaching(parent, includers, seen + (name,))
    return bases


FONT_FOR_BASE = {"base_admin.html": "bi", "base_public.html": "ti"}


def test_no_template_uses_an_icon_font_its_layout_does_not_load():
    """Including a partial in a page under the other base is enough to do this, so the check
    follows includes rather than only ``extends``."""
    includers = _includers()
    offenders = []
    for path in TEMPLATES.rglob("*.html"):
        name = str(path.relative_to(TEMPLATES))
        text = path.read_text()
        used = {font for font in ("bi", "ti") if f'class="{font} {font}-' in text}
        for base in _bases_reaching(name, includers):
            wrong = used - {FONT_FOR_BASE[base]}
            if wrong:
                offenders.append(f"{name} uses {sorted(wrong)} but renders under {base}")
    assert not offenders, "\n".join(offenders)


# --- the review menu ------------------------------------------------------------------


@pytest.fixture
def alma_nine():
    return AlmaLinuxRelease.objects.get_or_create(major=9, defaults={"supported": True})[0]


def _in_group(username, name):
    user = User.objects.create_user(username, password="pw")
    group, _ = Group.objects.get_or_create(name=name)
    user.groups.add(group)
    return user


@pytest.mark.parametrize(
    "group,expected", [("reviewer", True), ("admin", True), ("certifier", False)],
)
def test_the_review_menu_matches_the_permission_it_links_to(client, group, expected):
    """The sidebar gated on "is in any group at all"; the views admit ``reviewer`` and ``admin``.
    Any other group was enough to be shown the links and not enough to follow them, and
    ``seed_devstack`` creates exactly such a group, so the demo data reproduced it: a Review
    section leading to a plain-text 403."""
    client.force_login(_in_group(f"chrome-{group}", group))
    body = client.get(reverse("accounts:dashboard")).content.decode()

    assert (reverse("review:queue") in body) is expected


def test_a_user_in_no_group_sees_no_review_menu(client):
    client.force_login(User.objects.create_user("chrome-nobody", password="pw"))

    assert reverse("review:queue") not in client.get(
        reverse("accounts:dashboard")
    ).content.decode()


# --- the catalog search box -----------------------------------------------------------


@pytest.fixture
def two_vendors(alma_nine):
    from lumina.hardware.models import ListingVersion, System

    for name in ("Dell Inc.", "HPE"):
        vendor, _ = Vendor.objects.get_or_create(
            name=name, defaults={"published": True, "verified": True},
        )
        system = System.objects.create(
            vendor=vendor, name=f"{name} R760", published=True,
        )
        ListingVersion.objects.create(listing_system=system, release=alma_nine)


def test_the_search_box_carries_the_filters_the_reader_already_chose(client, two_vendors):
    """Both headers post ``hx-get="{{ request.path }}"``, which is path-only, so HTMX replaces the
    whole query string with that form's fields. The form held one field, so typing in the search
    box cleared every facet the reader had picked. The filter panel carries ``q`` the other way,
    which is why only one direction of one screen lost anything."""
    body = client.get(
        reverse("hardware:browse"), {"q": "R760", "vendor": ["dell-inc", "hpe"]},
    ).content.decode()

    header = body[body.index('id="catalog-search"') - 900:body.index('id="catalog-search"')]
    assert '<input type="hidden" name="vendor" value="dell-inc">' in header
    assert '<input type="hidden" name="vendor" value="hpe">' in header, (
        "a repeated facet key must produce one hidden input per value, not just the last"
    )


def test_the_search_box_does_not_carry_the_page_number(client, two_vendors):
    """A new search is a new result set; page 7 of the old one is not where to land."""
    body = client.get(reverse("hardware:browse"), {"q": "R760", "page": "7"}).content.decode()

    assert 'name="page"' not in body[:body.index('id="catalog-search"')]


# --- the header -------------------------------------------------------------------


def test_the_public_header_offers_a_labelled_dashboard_link(client):
    """Reported: it is not obvious you click your username to reach the dashboard. A signed-in
    person on a public page now gets a Dashboard link that reads as one, not just their name."""
    User.objects.create_user("navigator", email="nav@example.com")
    client.force_login(User.objects.get(username="navigator"))

    body = client.get(reverse("core:home")).content.decode()

    assert "Dashboard</a>" in body, "the header should offer a link labelled Dashboard"
    assert reverse("accounts:dashboard") in body


def test_no_dashboard_link_for_a_signed_out_visitor(client):
    body = client.get(reverse("core:home")).content.decode()

    assert "Dashboard</a>" not in body
    assert "Sign in" in body
