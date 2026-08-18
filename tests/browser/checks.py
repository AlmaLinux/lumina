"""Page-agnostic assertions, written once and pointed at any page.

Each one names what it found rather than reporting that something, somewhere, is wrong. A layout
failure that says "the page scrolls 27px sideways" sends somebody hunting; one that says
"div.text-end.flex-shrink-0 extends to 1467 in a 1440 viewport, containing 'and a new vendor'"
points at the line to change. The difference decides whether these get fixed or get skipped.

Each also reports how much it looked at, and the callers assert a floor. An assertion that
inspected nothing passes, and passing for that reason is worse than not existing: it reads on the
dashboard exactly like coverage.
"""
from __future__ import annotations

_OVERFLOW_JS = """() => {
    const limit = document.documentElement.clientWidth;
    const bad = [];
    let inspected = 0;
    for (const el of document.querySelectorAll('body *')) {
        const rect = el.getBoundingClientRect();
        if (rect.width === 0 || rect.height === 0) continue;
        inspected++;
        // Right edge only. An element parked off the left, like the off-canvas sidebar at
        // ``left: -240px``, is deliberate and creates no scrollbar: in a left-to-right document
        // the scroll width grows to the right. Flagging it reported the sidebar on every narrow
        // page and buried the real finding.
        if (rect.right > limit + 0.5) {
            // Only the outermost offender in any chain. A container that overflows drags its
            // children with it, and listing all of them buries the one that has to change.
            if (bad.some(b => b.el.contains(el))) continue;
            bad.push({el});
        }
    }
    return {
        limit, inspected,
        offenders: bad.map(b => ({
            tag: b.el.tagName.toLowerCase(),
            cls: (b.el.className || '').toString().trim(),
            right: Math.round(b.el.getBoundingClientRect().right),
            width: Math.round(b.el.getBoundingClientRect().width),
            text: (b.el.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 60),
        })),
    };
}"""


def assert_no_horizontal_overflow(page, *, minimum_elements: int = 50) -> None:
    """Nothing sticks out sideways.

    The signature of a container that has escaped its column, which is what "why does it look
    awful now" looked like, and of prose in a box that refuses to shrink.
    """
    result = page.evaluate(_OVERFLOW_JS)
    assert result["inspected"] >= minimum_elements, (
        f"only {result['inspected']} elements were laid out; the page is probably empty, "
        "so this check proved nothing"
    )
    if result["offenders"]:
        lines = "\n".join(
            f"  {o['tag']}.{o['cls'] or '(no class)'} extends to {o['right']} "
            f"(width {o['width']}) in a {result['limit']} viewport: {o['text']!r}"
            for o in result["offenders"]
        )
        raise AssertionError(f"{len(result['offenders'])} element(s) overflow sideways:\n{lines}")


_INVISIBLE_JS = """(selectors) => {
    const out = [];
    let inspected = 0;
    for (const selector of selectors) {
        for (const el of document.querySelectorAll(selector)) {
            inspected++;
            const rect = el.getBoundingClientRect();
            const style = getComputedStyle(el);
            if (rect.width === 0 || rect.height === 0 || style.visibility === 'hidden'
                || style.display === 'none' || parseFloat(style.opacity) === 0) {
                out.push({
                    selector,
                    text: (el.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 50),
                    why: rect.height === 0 ? 'zero height'
                         : rect.width === 0 ? 'zero width' : style.display === 'none'
                         ? 'display:none' : style.visibility === 'hidden'
                         ? 'visibility:hidden' : 'opacity:0',
                });
            }
        }
    }
    return {out, inspected};
}"""


def assert_visible(page, selectors: list[str], *, minimum_elements: int = 1) -> None:
    """Everything matching these selectors is actually on the screen.

    Rendered and invisible is the failure mode this project keeps hitting: a fieldset whose
    contents were dropped, a glyph from a font the layout does not load, a card body with nothing
    in it. All of them are present in the HTML.
    """
    result = page.evaluate(_INVISIBLE_JS, selectors)
    assert result["inspected"] >= minimum_elements, (
        f"{selectors} matched {result['inspected']} elements, fewer than the {minimum_elements} "
        "expected, so nothing was really checked"
    )
    assert not result["out"], "rendered but not visible:\n" + "\n".join(
        f"  {item['selector']}: {item['why']} {item['text']!r}" for item in result["out"]
    )


_ICON_JS = """() => {
    const out = [];
    let inspected = 0;
    for (const el of document.querySelectorAll('i[class*="ti-"], i[class*="bi-"]')) {
        // Only icons that are on the screen. The navbar's hamburger is d-lg-none, so at a desktop
        // width it is correctly 0x0, and counting that as a missing glyph would make the check
        // fire on every page that has a responsive layout.
        if (!el.checkVisibility || !el.checkVisibility()) continue;
        inspected++;
        const rect = el.getBoundingClientRect();
        if (rect.width < 4 || rect.height < 4) {
            out.push({cls: el.className, w: Math.round(rect.width), h: Math.round(rect.height)});
        }
    }
    return {out, inspected};
}"""


def assert_icons_render(page, *, minimum_elements: int = 1) -> None:
    """Every icon has a glyph behind it.

    The two layouts load different icon fonts. A Bootstrap Icons class on a Tabler page is a blank
    space of exactly the size a designer might have wanted there, which is why four of them lived
    on the submitter's listing form without anybody filing a bug.
    """
    result = page.evaluate(_ICON_JS)
    assert result["inspected"] >= minimum_elements, (
        f"the page rendered {result['inspected']} icons, fewer than the {minimum_elements} "
        "expected, so this proved nothing"
    )
    assert not result["out"], "icons with no glyph (wrong font for this layout?):\n" + "\n".join(
        f"  {item['cls']} is {item['w']}x{item['h']}" for item in result["out"]
    )


def assert_named_controls_have_a_form(page) -> None:
    """Every control that looks like it submits a value belongs to a form.

    The browser is the only oracle. Form ownership is decided by the parser after it has repaired
    whatever nesting the template produced, so ``form form`` matches nothing even on a page whose
    source really does nest them. What is observable is where each control ended up.
    """
    orphans = page.evaluate("""() => {
        const out = [];
        for (const el of document.querySelectorAll('input, select, textarea, button')) {
            if (el.type === 'button' || !el.name) continue;
            // ``.pane-toggle`` is view state, not data: a radio pair deciding which half of a
            // card is on screen. Radios need a shared name to be mutually exclusive, so unlike
            // the disclosure checkboxes these cannot simply go unnamed, and a name is the only
            // reason they look like data at all. The class is the declaration that they are not.
            if (el.classList.contains('pane-toggle')) continue;
            if (!el.form) out.push(el.tagName.toLowerCase() + '[name=' + el.name + ']');
        }
        return out;
    }""")
    assert orphans == [], (
        "these post nothing because they belong to no form: " + ", ".join(orphans)
    )


def disclosure_controls(page) -> list[dict]:
    """Every CSS-only disclosure on the page, paired with the label that drives it.

    Paired here rather than in each test because the pairing is the fragile part: the mechanism is
    a hidden checkbox and a sibling selector, so a wrapper div added between them breaks the
    reveal while leaving every element present and every server-side assertion true.
    """
    return page.evaluate("""() => {
        const out = [];
        for (const toggle of document.querySelectorAll('.reveal-toggle')) {
            const fields = toggle.parentElement.querySelector(':scope > .reveal-fields');
            out.push({
                id: toggle.id,
                hasFields: !!fields,
                labelFound: !!document.querySelector('label[for="' + toggle.id + '"]'),
            });
        }
        return out;
    }""")


def assert_every_disclosure_reveals(page, *, expected: int) -> None:
    """Each hidden-checkbox disclosure hides its fields, shows them when its label is clicked, and
    hides them again.

    The one part of this interface with no server-side evidence at all: the fields are in the HTML
    either way, so every string assertion passes in both states. Whether the label does anything
    depends on a stylesheet being linked by *this* layout and a sibling selector still matching,
    and the only way to learn that is to render it and click.
    """
    controls = disclosure_controls(page)
    assert len(controls) == expected, (
        f"expected {expected} disclosure controls, found {len(controls)}: "
        f"{[c['id'] for c in controls]}"
    )
    for control in controls:
        assert control["hasFields"], (
            f"{control['id']} has no .reveal-fields as a sibling, so the selector that shows them "
            "cannot match. Something was probably wrapped in a new div."
        )
        assert control["labelFound"], f"{control['id']} has no label, so nobody can click it"
        fields = page.locator(f"#{control['id']} ~ .reveal-fields").first
        label = page.locator(f"label[for='{control['id']}']").first
        assert not fields.is_visible(), f"{control['id']} starts open; it should start closed"
        label.click()
        assert fields.is_visible(), (
            f"clicking the label for {control['id']} revealed nothing. Either the stylesheet "
            "holding .reveal-* is not linked by this layout, or the selector no longer matches."
        )
        label.click()
        assert not fields.is_visible(), f"{control['id']} does not close again"
