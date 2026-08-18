"""The public navbar, with a signed-in name in it.

Reported from the dev site once it was on Keycloak: the header's items were drawn on top of each
other, with the brand printed underneath the first nav links. The username was a 38-character hash
at the time (mozilla-django-oidc's default, since fixed), so the obvious reading was "the hash is too
long". It was not the cause. ``navbar-expand-md`` sets ``flex-wrap: nowrap``, and a flex item's
automatic minimum size is its own content, so nothing in that row could shrink: the collision was
always available at a narrow enough window, and a long name only brought the window up to 1400px.

Nothing server-side can see this. The markup is correct and always was; the geometry is not, and it
exists only once a browser has laid the row out.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser

# The one that was reported, plus one nobody would choose. An identity provider's username is
# arbitrary text, so the layout has to hold for arbitrary text rather than for names we like.
NAMES = [
    pytest.param("jwright", id="ordinary"),
    pytest.param("bXxZnD3rKl4TGDk-kYTWENYZY9k", id="the-reported-hash"),
    pytest.param("a-very-long-federated-account-name-that-nobody-would-choose", id="absurd"),
]

# The row's items, in the order they are drawn. Every pair is checked, so a collision anywhere in
# here is reported with both offenders named.
ROW = ".lumina-public-navbar .navbar-brand, .lumina-public-navbar .nav-link, \
.lumina-public-navbar .lumina-session-controls .btn"

_BOXES_JS = """(selector) => {
    const out = [];
    for (const el of document.querySelectorAll(selector)) {
        const r = el.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) continue;   // collapsed at this viewport, not drawn
        out.push({
            label: (el.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 34)
                   || el.tagName.toLowerCase(),
            left: Math.round(r.left), right: Math.round(r.right),
            top: Math.round(r.top), bottom: Math.round(r.bottom),
        });
    }
    return out;
}"""


def _overlaps(a, b):
    """Do two boxes share any area? Touching edges do not count."""
    return (a["left"] < b["right"] and b["left"] < a["right"]
            and a["top"] < b["bottom"] and b["top"] < a["bottom"])


@pytest.mark.parametrize("name", NAMES)
# Widths where the row is actually laid out side by side. The navbar collapses below xl (1200px),
# and a collapsed row has nothing to collide, so testing 1024 here measured nothing: the guard below
# said so out loud rather than passing quietly, which is the only reason this list is right.
@pytest.mark.parametrize("width,height", [(1920, 1080), (1440, 900), (1280, 800), (1200, 800)],
                         ids=["wide", "desktop", "narrow-desktop", "at-the-breakpoint"])
def test_the_header_row_never_draws_on_top_of_itself(page, visit, sign_in, submitter,
                                                     name, width, height):
    """Whatever the username, and at every width where the row is expanded.

    Below the ``md`` breakpoint the row collapses behind the toggler and there is nothing to
    collide, so the widths here are the ones where it is laid out side by side.
    """
    submitter.username = name
    submitter.save(update_fields=["username"])
    sign_in(submitter)
    page.set_viewport_size({"width": width, "height": height})
    visit("core:home")

    boxes = page.evaluate(_BOXES_JS, ROW)
    assert len(boxes) >= 7, (
        f"only {len(boxes)} header items were laid out, so this proved nothing: {boxes}"
    )
    collisions = [
        (a["label"], b["label"])
        for i, a in enumerate(boxes) for b in boxes[i + 1:] if _overlaps(a, b)
    ]
    detail = "\n".join(f"  {a!r} overlaps {b!r}" for a, b in collisions)
    assert not collisions, (
        f"header items are drawn on top of each other at {width}x{height} "
        f"with username {name!r}:\n{detail}"
    )


@pytest.mark.parametrize("name", NAMES)
def test_the_name_is_truncated_rather_than_allowed_to_push(page, visit, sign_in, submitter, name):
    """A ceiling on the name, with the whole value still reachable.

    Truncated in CSS rather than by ``|truncatechars`` in the template, so the full name stays in
    the DOM and in the title attribute where a person can still read it.
    """
    submitter.username = name
    submitter.save(update_fields=["username"])
    sign_in(submitter)
    page.set_viewport_size({"width": 1440, "height": 900})
    visit("core:home")

    holder = page.locator(".lumina-public-navbar .lumina-user-name")
    assert holder.inner_text().strip() == name, "the full name should still be in the DOM"
    assert page.locator(".lumina-public-navbar .btn[title]").first.get_attribute("title") == name

    width = page.evaluate(
        "() => document.querySelector('.lumina-public-navbar .lumina-user-name')"
        ".getBoundingClientRect().width")
    # 12rem is the cap in lumina-public.css; allow a pixel of rounding.
    assert width <= 12 * 16 + 1, f"the name box is {width}px wide, so the cap is not applying"


LONG = "a-very-long-federated-account-name-that-nobody-would-choose"


def test_sign_out_stays_on_screen_however_long_the_name_is(page, visit, sign_in, submitter):
    """The control that gets squeezed off the edge must never be the way back out.

    ``flex-shrink: 0`` on the session controls is what keeps this true, and without a test it is
    the kind of line somebody tidies away.
    """
    submitter.username = LONG
    submitter.save(update_fields=["username"])
    sign_in(submitter)
    page.set_viewport_size({"width": 1200, "height": 800})
    visit("core:home")

    button = page.locator(".lumina-public-navbar button:has-text('Sign out')")
    assert button.is_visible()
    box = button.bounding_box()
    assert box is not None and box["width"] > 0
    assert box["x"] + box["width"] <= 1200 + 1, (
        f"Sign out is off the right edge: ends at {box['x'] + box['width']} in a 1200 viewport"
    )


@pytest.mark.parametrize("width,height", [(1024, 768), (390, 844)], ids=["laptop", "phone"])
def test_below_the_breakpoint_the_row_collapses_behind_the_toggler(page, visit, sign_in, submitter,
                                                                  width, height):
    """Which is why the widths above start at 1200.

    Worth asserting rather than assuming: the breakpoint moved from md to xl to stop the row being
    laid out expanded at widths it could not fill, and if it ever moves back, the geometry test above
    would start measuring a collapsed row and pass without checking anything.
    """
    submitter.username = LONG
    submitter.save(update_fields=["username"])
    sign_in(submitter)
    page.set_viewport_size({"width": width, "height": height})
    visit("core:home")

    toggler = page.locator(".lumina-public-navbar .navbar-toggler")
    assert toggler.is_visible(), "the row is expanded at a width it cannot fill"
    assert not page.locator(".lumina-public-navbar .nav-link").first.is_visible()

    # And opening it reaches the way out, so collapsing is not a trap.
    toggler.click()
    page.wait_for_timeout(400)   # the collapse transition
    assert page.locator(".lumina-public-navbar button:has-text('Sign out')").is_visible()
