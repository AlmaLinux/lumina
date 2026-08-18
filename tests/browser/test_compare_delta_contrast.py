"""The better/worse delta chips on the compare page have to be legible.

Reported as poor contrast. The chips are green or red text on a faint tint of the same colour, and
Tabler's green/red *text* tokens are tuned for white: on the tint they fell to ~2.4:1 (better) and
~3.9:1 (worse), under WCAG AA's 4.5. This measures the real rendered colours, and because the chip
background is translucent (rgba at 12% alpha) it composites that over the card behind it first, the
way the browser paints it, rather than scoring the semi-transparent colour as if it were solid.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser

AA = 4.5

# Injected with the real classes onto a page that has loaded lumina-public.css, so the values under
# test are the stylesheet's, not a copy. Composited over white, which is the card colour the chip
# actually sits on in the compare table.
_CONTRAST_JS = """
(cls) => {
    const el = document.createElement('span');
    el.className = cls;
    el.textContent = '-5% worse';
    document.body.appendChild(el);
    const cs = getComputedStyle(el);
    const nums = (s) => (s.match(/[\\d.]+/g) || []).map(Number);
    const fg = nums(cs.color);                 // opaque text colour
    const bgc = nums(cs.backgroundColor);      // may be rgba with alpha < 1
    const a = bgc.length > 3 ? bgc[3] : 1;
    const over = [0, 1, 2].map((i) => Math.round(a * bgc[i] + (1 - a) * 255));  // over white
    const lum = ([r, g, b]) => {
        const f = (c) => { c /= 255; return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; };
        return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
    };
    const L1 = lum(fg), L2 = lum(over);
    const hi = Math.max(L1, L2), lo = Math.min(L1, L2);
    el.remove();
    return { ratio: (hi + 0.05) / (lo + 0.05), color: cs.color, bg: cs.backgroundColor };
}
"""


@pytest.mark.parametrize("cls", ["compare-delta is-better", "compare-delta is-worse"],
                         ids=["better", "worse"])
def test_a_delta_chip_is_legible(page, visit, cls):
    visit("core:home")

    result = page.evaluate(_CONTRAST_JS, cls)

    assert result["ratio"] >= AA, (
        f"{cls!r}: contrast {result['ratio']:.2f} is below WCAG AA {AA} "
        f"({result['color']} on {result['bg']} over white)"
    )
