"""The review-queue tabs are linkable: the open tab is written to `?tab=` and restored from it.

Bootstrap's tabs are otherwise pure client state, so a reviewer could neither send a colleague to
"the software queue" nor reload onto the tab they were reading. tab-url.js closes that gap, and it
is the sort of enhancement a server-side test cannot see at all.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser


def test_a_tab_query_param_opens_that_tab(page, visit, sign_in, reviewer):
    sign_in(reviewer)
    visit("review:queue", tab="software")

    page.wait_for_selector("#tab-software.active")
    assert page.locator("#tab-software").is_visible()
    assert not page.locator("#tab-submissions").is_visible()


def test_clicking_a_tab_writes_it_to_the_url(page, visit, sign_in, reviewer):
    sign_in(reviewer)
    visit("review:queue")
    # The tab the markup opens with needs no marker in the URL to be reachable.
    assert "tab=" not in page.url

    page.get_by_role("tab", name="Vendors", exact=False).click()
    page.wait_for_url("**/review/?tab=vendors")
    assert page.locator("#tab-vendors").is_visible()


def test_the_url_survives_a_reload(page, visit, sign_in, reviewer):
    """The point of writing it: the tab comes back where the reader left it."""
    sign_in(reviewer)
    visit("review:queue")
    page.get_by_role("tab", name="Benchmark runs", exact=False).click()
    page.wait_for_url("**/review/?tab=benchmark-runs")

    page.reload(wait_until="networkidle")
    page.wait_for_selector("#tab-benchmark-runs.active")
    assert page.locator("#tab-benchmark-runs").is_visible()


def test_an_unknown_tab_falls_back_to_the_default(page, visit, sign_in, reviewer):
    """A stale or mistyped slug must not leave every pane closed."""
    sign_in(reviewer)
    visit("review:queue", tab="does-not-exist")

    page.wait_for_selector("#tab-submissions.active")
    assert page.locator("#tab-submissions").is_visible()
