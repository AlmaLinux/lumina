"""The page loads entirely from this origin, and the CSP does not block its own assets.

Two things a server-side test cannot see. First, that self-hosting the former CDN and font-host
assets actually took: a stray external URL only shows up as a request the browser makes to another
domain. Second, that the strict Content-Security-Policy admits the vendored scripts and the nonce'd
inline blocks rather than quietly blocking them, which surfaces only as a console violation and a
dead page. Both are checked here against a real browser.
"""
from __future__ import annotations

from urllib.parse import urlparse

import pytest

pytestmark = pytest.mark.browser


def _offsite(requests, allowed_host):
    out = []
    for url in requests:
        if url.startswith("data:") or url.startswith("blob:"):
            continue
        if urlparse(url).netloc and urlparse(url).netloc != allowed_host:
            out.append(url)
    return out


def test_a_public_page_makes_no_offsite_requests(page, visit, live_server):
    host = urlparse(live_server.url).netloc
    seen = []
    page.on("request", lambda r: seen.append(r.url))
    violations = []
    page.on("console", lambda m: violations.append(m.text)
            if "content security policy" in m.text.lower() else None)

    visit("core:home")
    page.wait_for_load_state("networkidle")

    offsite = _offsite(seen, host)
    assert not offsite, "the page fetched from another domain:\n  " + "\n  ".join(offsite)
    assert not violations, "the CSP blocked something on the page:\n  " + "\n  ".join(violations)


def test_the_vendored_font_and_icon_assets_actually_load(page, visit, live_server):
    """A 404 on a vendored asset would not fail the no-offsite check, so it is asserted directly:
    every request the home page makes returns under 400."""
    host = urlparse(live_server.url).netloc
    failed = []
    page.on("response", lambda r: failed.append((r.url, r.status))
            if urlparse(r.url).netloc == host and r.status >= 400 else None)

    visit("core:home")
    page.wait_for_load_state("networkidle")

    assert not failed, "a same-origin asset failed to load:\n  " + "\n  ".join(
        f"{u} -> {s}" for u, s in failed)


def test_a_nonced_inline_script_is_not_blocked(page, visit, sign_in, submitter):
    """The dashboard carries an inline nonce'd <script>. If the nonce did not match the header the
    browser would refuse to run it and log a CSP violation, so a page that has an inline script and
    logs no violation is the proof the nonce is honored. Behavioural row-filtering is deliberately
    not asserted here: it depends on seeded rows, while the nonce does not."""
    from tests.browser import fixtures

    fixtures.make_run(submitter)
    sign_in(submitter)

    violations = []

    def watch(msg):
        text = msg.text.lower()
        if "content security policy" in text or "refused to execute inline script" in text:
            violations.append(msg.text)

    page.on("console", watch)
    page.on("pageerror", lambda exc: violations.append(str(exc)))

    visit("accounts:dashboard")
    page.wait_for_load_state("networkidle")

    # The premise: there really is an inline script on this page, so a clean console means
    # something.
    inline = page.evaluate(
        "() => Array.from(document.scripts).filter(s => !s.src && s.textContent.trim()).length"
    )
    assert inline >= 1, "expected an inline script on the dashboard for this test to be meaningful"
    assert not violations, "the CSP blocked the inline script:\n  " + "\n  ".join(violations)
