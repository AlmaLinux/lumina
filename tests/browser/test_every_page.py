"""One sweep over every page worth looking at, under both layouts and three viewports.

The cheapest thing a browser buys is breadth. Each of these checks is page-agnostic, so adding a
page to the table below costs one line and gets it checked for sideways overflow, missing glyphs,
controls attached to no form, and cards rendered with nothing in them.

The table is the point. Two of this project's reported breakages were a partial behaving
differently under the two base layouts, which no per-page test would find because each page was
fine on its own terms.
"""
from __future__ import annotations

import pytest

from tests.browser import checks

pytestmark = pytest.mark.browser

# (id, url name, args-from-fixtures, who has to be signed in)
PAGES = [
    ("home", "core:home", (), None),
    ("hardware-browse", "hardware:browse", (), None),
    ("hardware-systems", "hardware:systems", (), None),
    ("hardware-components", "hardware:components", (), None),
    ("software-browse", "software:browse", (), None),
    ("leaderboards", "benchmarks:index", (), None),
    ("hardware-detail", "hardware:detail", ("system.slug",), None),
    # Signed in as its submitter: an unpublished run is a 404 to everybody else, and
    # "the page 404s" is not the failure this sweep is looking for.
    ("run-detail", "results:run_detail", ("run.uuid",), "submitter"),
    ("dashboard", "accounts:dashboard", (), "submitter"),
    ("propose-listing", "results:propose_listing", ("run.uuid",), "submitter"),
    ("review-queue", "review:queue", (), "reviewer"),
    ("review-run", "review:run_detail", ("run.pk",), "reviewer"),
]

VIEWPORTS = [("desktop", 1440, 900), ("laptop", 1024, 768), ("phone", 390, 844)]


@pytest.fixture
def scene(pending_run, published_system, submitter, reviewer):
    """One set of data all the pages can be rendered against.

    ``pending_run`` is submitted by the Intel engineer, so the reviewer's page has its component
    controls; the submitter's own pages need a run they own, so they get one of their own.
    """
    from tests.browser.fixtures import make_run

    own = make_run(submitter)
    return {
        "run": pending_run, "own_run": own, "system": published_system,
        "submitter": submitter, "reviewer": reviewer,
    }


def _open(visit, sign_in, scene, spec, who):
    if who:
        sign_in(scene[who])
    run = scene["own_run"] if who == "submitter" else scene["run"]
    args = []
    for source in spec:
        obj, attr = source.split(".")
        args.append(getattr(run if obj == "run" else scene["system"], attr))
    return args


# Narrow-viewport failures this sweep found on its first run, kept as recorded work rather than
# deleted or quietly excluded. ``strict=True``, so fixing one turns the xpass into a failure and
# whoever fixed it removes the entry, instead of the marker outliving the bug.
#
# One cause left. Wide tables do not scroll inside their column: they sit in grid columns and flex
# items, which default to ``min-width: auto`` and so grow to fit their content, leaving the
# ``.table-responsive`` around them with nothing to scroll.
#
# The other cause is gone. The public navbar's link row used to stay expanded down to 768 and ran
# past the edge at a laptop width; it now collapses below 1200 and wraps rather than overlapping.
# Removing those entries corrected the record as well as the layout: the ``laptop`` entry for
# run-detail blamed its DIMM table, and that page now passes at 1024 without the table changing at
# all, so the navbar had been the only offender at that width the whole time. The DIMM table really
# does overflow, but only at phone width, where it is still listed.
KNOWN_NARROW = {
    ("laptop", "review-run"): "the component badge column and the DIMM table",
    ("phone", "hardware-detail"): "the AlmaLinux compatibility table",
    ("phone", "run-detail"): "the results table and the DIMM table",
    ("phone", "dashboard"): "the runs table and a w-auto input",
    ("phone", "review-queue"): "the queue table",
    ("phone", "review-run"): "the results table and the DIMM table",
}


@pytest.mark.parametrize("name,url_name,spec,who", PAGES, ids=[p[0] for p in PAGES])
def test_the_page_holds_together(page, visit, sign_in, scene, name, url_name, spec, who):
    """One navigation, every page-agnostic check, at every viewport.

    Combined rather than one test per check because these now run with the rest of the suite on
    every change, and thirty-nine page loads to make thirteen pages' worth of assertions is a
    minute nobody gets back. Resizing relayouts without reloading, which is all a media query
    needs.

    Every failure is collected and reported together. A page that overflows at two widths and also
    has a dead glyph should say so once, not three runs in a row.
    """
    args = _open(visit, sign_in, scene, spec, who)
    visit(url_name, *args)

    failures: dict[str, str] = {}
    for label, width, height in VIEWPORTS:
        page.set_viewport_size({"width": width, "height": height})
        try:
            checks.assert_no_horizontal_overflow(page, minimum_elements=20)
        except AssertionError as exc:
            failures[label] = str(exc)
    page.set_viewport_size({"width": VIEWPORTS[0][1], "height": VIEWPORTS[0][2]})
    for check, run_check in (
        ("icons", lambda: checks.assert_icons_render(page, minimum_elements=1)),
        ("forms", lambda: checks.assert_named_controls_have_a_form(page)),
    ):
        try:
            run_check()
        except AssertionError as exc:
            failures[check] = str(exc)

    known = {label for (label, page_name) in KNOWN_NARROW if page_name == name}
    unexpected = {k: v for k, v in failures.items() if k not in known}
    assert not unexpected, f"{name}:\n" + "\n".join(
        f"[{k}] {v}" for k, v in unexpected.items()
    )
    # Strict, like an xfail. A fixed page has to lose its entry, or the list becomes a record of
    # what used to be broken and stops being a to-do.
    fixed = known - set(failures)
    assert not fixed, (
        f"{name} no longer fails at {sorted(fixed)}. Remove it from KNOWN_NARROW."
    )
