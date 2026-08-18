"""The submitter's dashboard, driven in a browser.

Two things here have no server-side evidence. The archive tabs are CSS-only, so both panes are in
the HTML whichever one is showing and every string assertion passes in both states. And the call
to action is a claim about reading order, which is a fact about the rendered page rather than
about the template's text.
"""
from __future__ import annotations

import pytest

from tests.browser import checks

pytestmark = pytest.mark.browser


# Each pane group is located by the radio group that drives it. Locating one by its
# heading text looks natural and is not stable: card copy elsewhere on the dashboard
# cross-references these sections by name, and ``has_text`` matches substrings anywhere
# in the card, so a second group starts matching and the locator fails strict mode.
def test_the_archive_tabs_switch(page, visit, sign_in, submitter, archived_and_active):
    """The pane control is a radio pair and a sibling selector. If the stylesheet stops being
    linked, or a wrapper appears between the radio and the pane, both panes show at once and
    nothing on the server side can tell."""
    sign_in(submitter)
    visit("accounts:dashboard")

    validations = page.locator(".pane-group:has(input[name='validation-pane'])")
    active = validations.locator(".pane-active")
    archived = validations.locator(".pane-archived")
    assert active.is_visible() and not archived.is_visible()

    page.locator("label[for='validation-pane-archived']").click()

    assert archived.is_visible() and not active.is_visible()

    page.locator("label[for='validation-pane-active']").click()

    assert active.is_visible() and not archived.is_visible()


def test_the_selected_archive_tab_is_highlighted(
    page, visit, sign_in, submitter, archived_and_active,
):
    """The reported bug: the panes switched but the tab underline stayed on Active. The highlight
    swap reached for the tabs as a direct sibling of the radios, but they sit inside .card-header,
    so it matched nothing. Asserted on the computed underline, since no class changes."""
    sign_in(submitter)
    visit("accounts:dashboard")

    group = page.locator(".pane-group:has(input[name='validation-pane'])")
    active_tab = group.locator("label.pane-tab-active")
    archived_tab = group.locator("label.pane-tab-archived")

    def underlined(loc) -> bool:
        # The highlight is a colored bottom border; an unselected tab's is transparent.
        color = loc.evaluate("el => getComputedStyle(el).borderBottomColor")
        return color != "rgba(0, 0, 0, 0)"

    assert underlined(active_tab) and not underlined(archived_tab)

    archived_tab.click()

    assert underlined(archived_tab) and not underlined(active_tab)


def test_archiving_a_run_moves_it(page, visit, sign_in, submitter, archived_and_active):
    """End to end: press the button, and the row leaves the active pane."""
    sign_in(submitter)
    visit("accounts:dashboard")
    before = page.locator(".pane-active tbody tr").count()

    page.get_by_role("button", name="Archive").first.click()
    page.wait_for_url("**/my/")

    assert page.locator(".pane-active tbody tr").count() == before - 1
    assert page.get_by_text("Archived. You can find it").is_visible()


def test_the_call_to_action_is_the_first_thing_on_the_page(
    page, visit, sign_in, submitter, archived_and_active,
):
    """Above the quick actions, and above the fold at a laptop height. Somebody opens this page to
    find out whether anything needs them."""
    sign_in(submitter)
    page.set_viewport_size({"width": 1440, "height": 768})
    visit("accounts:dashboard")

    block = page.locator(".card.border-warning", has_text="Waiting on you")
    assert block.is_visible()
    box = block.bounding_box()
    assert box["y"] < 768, "the call to action is below the fold"

    quick_actions = page.get_by_role("heading", name="Submit hardware").last.bounding_box()
    assert box["y"] < quick_actions["y"]


def test_it_says_nothing_when_there_is_nothing(page, visit, sign_in, submitter):
    """A region that is always there and usually empty is a region people learn to skip."""
    sign_in(submitter)
    visit("accounts:dashboard")

    assert page.locator(".card.border-warning", has_text="Waiting on you").count() == 0


def test_the_dashboard_still_holds_together(page, visit, sign_in, submitter, archived_and_active):
    sign_in(submitter)
    visit("accounts:dashboard")

    checks.assert_no_horizontal_overflow(page, minimum_elements=20)
    checks.assert_icons_render(page, minimum_elements=1)
    checks.assert_named_controls_have_a_form(page)
