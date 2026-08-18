"""The browse buttons in the home header, on a narrow screen.

Reported: collapsed to a mobile view, the three Browse buttons clobbered each other and their
spacing was uneven. The cause was a ``col-auto`` cramped beside the title with ``me-1`` on each
button. ``me-1`` is a horizontal margin only, so when the buttons wrapped they stacked with a 2px
vertical gap (they looked fused), while side by side they had a 4px gap: cramped and inconsistent
between the two axes. A flex row with a single ``gap-2`` gives the same 8px in both directions.

Nothing server-side sees this. The markup was always valid; the spacing exists only once a browser
lays the row out at a given width, so it is measured here in a real browser.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser

_BOXES_JS = """
() => Array.from(document.querySelectorAll('.page-header .btn')).map((el) => {
    const r = el.getBoundingClientRect();
    return {text: el.textContent.trim(),
            left: Math.round(r.left), right: Math.round(r.right),
            top: Math.round(r.top), bottom: Math.round(r.bottom)};
})
"""

# gap-2 is 0.5rem = 8px. The floor rules out the old cramped gaps (2px stacked, 4px me-1); the
# spread bound is the consistency the report asked for. Generous enough not to be brittle about
# sub-pixel rounding.
MIN_GAP = 6
MAX_SPREAD = 3


def _rows(boxes):
    """Group buttons into visual rows by their top edge (within a few px)."""
    rows = []
    for b in sorted(boxes, key=lambda b: (b["top"], b["left"])):
        if rows and abs(b["top"] - rows[-1][0]["top"]) <= 4:
            rows[-1].append(b)
        else:
            rows.append([b])
    return rows


def _gaps(boxes):
    """Every gap between adjacent buttons: horizontal within a row, vertical between rows."""
    rows = _rows(boxes)
    gaps = []
    for row in rows:
        ordered = sorted(row, key=lambda b: b["left"])
        gaps += [ordered[i + 1]["left"] - ordered[i]["right"] for i in range(len(ordered) - 1)]
    for i in range(len(rows) - 1):
        gaps.append(min(b["top"] for b in rows[i + 1]) - max(b["bottom"] for b in rows[i]))
    return gaps


@pytest.mark.parametrize("width,height", [(390, 844), (600, 800), (768, 1024), (1280, 900)],
                         ids=["phone", "small", "tablet", "desktop"])
def test_the_browse_buttons_are_spaced_evenly_and_never_cramped(page, visit, width, height):
    page.set_viewport_size({"width": width, "height": height})
    visit("core:home")

    boxes = page.evaluate(_BOXES_JS)
    assert len(boxes) == 3, f"expected three browse buttons, saw {[b['text'] for b in boxes]}"

    # None overlapping, whatever the layout.
    clashes = [
        (a["text"], b["text"])
        for i, a in enumerate(boxes) for b in boxes[i + 1:]
        if a["left"] < b["right"] and b["left"] < a["right"]
        and a["top"] < b["bottom"] and b["top"] < a["bottom"]
    ]
    assert not clashes, f"buttons overlap at {width}x{height}: {clashes}"

    gaps = _gaps(boxes)
    assert gaps, "the three buttons collapsed onto one point"
    assert min(gaps) >= MIN_GAP, (
        f"buttons are cramped at {width}x{height}: gaps={gaps} (floor {MIN_GAP}px)"
    )
    assert max(gaps) - min(gaps) <= MAX_SPREAD, (
        f"spacing is uneven at {width}x{height}: gaps={gaps}"
    )
