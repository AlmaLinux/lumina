"""Status badges have to be legible, which is a fact about rendered colors, not markup.

Reported from the dev site: the PASS badge's text was hard to read. The cause was white text on the
bright brand green (#68bc11), a contrast of ~2.1 against WCAG AA's 4.5. Nothing server-side can see
this - the template is correct, the class names are right, and the failure exists only once a browser
has resolved the CSS cascade into actual foreground and background colors.

So this measures the real thing: the real classes the templates use, styled by the real stylesheet
that base_public.html loads, with contrast computed from getComputedStyle the way a browser paints it.
The badges are injected rather than fished out of a page, because which pages happen to show a PASS
or a FAIL depends on fixture data, while the CSS rule under test does not.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser

# The verdict and status badges as the templates spell them: PASS/FAIL use both bg-* and text-bg-*
# spellings across the templates, and warning rounds out the brand-colored set.
BADGES = [
    ("PASS via bg-success", "badge bg-success", "PASS"),
    ("PASS via text-bg-success", "badge text-bg-success", "PASS"),
    ("FAIL via bg-danger", "badge bg-danger", "FAIL"),
    ("FAIL via text-bg-danger", "badge text-bg-danger", "FAIL"),
    ("warning", "badge bg-warning", "held"),
]

# WCAG AA for normal text. Badge text is small, so this is the floor that applies.
AA = 4.5

# Contrast of two colors given as [r,g,b], per the WCAG relative-luminance formula. Kept in the page
# so it runs against the browser's own resolved rgb() values rather than a Python guess at them.
_CONTRAST_JS = """
([cls, text]) => {
    const el = document.createElement('span');
    el.className = cls;
    el.textContent = text;
    document.body.appendChild(el);
    const cs = getComputedStyle(el);
    const parse = (s) => s.match(/[\\d.]+/g).slice(0, 3).map(Number);
    const lum = ([r, g, b]) => {
        const f = (c) => { c /= 255; return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; };
        return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
    };
    const fg = lum(parse(cs.color));
    const bg = lum(parse(cs.backgroundColor));
    const hi = Math.max(fg, bg), lo = Math.min(fg, bg);
    const ratio = (hi + 0.05) / (lo + 0.05);
    const out = { ratio, color: cs.color, background: cs.backgroundColor };
    el.remove();
    return out;
}
"""


@pytest.mark.parametrize("label,cls,text", BADGES, ids=[b[0] for b in BADGES])
def test_a_status_badge_is_legible(page, visit, label, cls, text):
    # Any public page loads base_public.html and therefore lumina-public.css, which is what styles
    # these classes. The home page is the cheapest.
    visit("core:home")

    result = page.evaluate(_CONTRAST_JS, [cls, text])

    assert result["ratio"] >= AA, (
        f"{label}: contrast {result['ratio']:.2f} is below WCAG AA {AA} "
        f"({result['color']} on {result['background']})"
    )
