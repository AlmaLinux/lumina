"""A partial rendered under both base layouts may only rely on CSS both layouts load.

Reported as a question: "on the review page for runs there is a checkbox beside 'memory
modules'. What does that checkbox do?" Nothing visible, and that was the bug. It is the "show
all N modules" toggle, styled to be invisible with its `<label>` as the visible control - and
the rules that hide it live in `lumina-public.css`, which `base_admin.html` does not link. So on
the review page the checkbox showed as a bare unexplained control, every module was already
listed because `.dimm-extra` was never hidden, and with more than four modules both label texts
rendered at once ("Show all 8 modules" and "Show fewer").

The same slip had just been repeated: the per-component "Not this part? Correct it" override was
added to the reviewer's page with its `.reveal-*` rules public-only, so a matched part's entry
fields were permanently open there - which is exactly the thing that override exists to prevent.

Two occurrences of one mistake, so this asserts the rule rather than the two instances. Both
were invisible to the suite because every existing test of those templates is server-side: the
markup was correct in both places, and only the stylesheet was missing.
"""
from __future__ import annotations

import re
from pathlib import Path

from django.conf import settings

TEMPLATES = Path(settings.BASE_DIR) / "templates"
STATIC_CSS = Path(settings.BASE_DIR) / "static" / "css"

BASES = ("base_public.html", "base_admin.html")


def _linked_stylesheets(base: str) -> set[str]:
    """The project stylesheets a base layout links, by file name."""
    body = (TEMPLATES / base).read_text()
    return set(re.findall(r"{%\s*static\s+'css/([^']+)'\s*%}", body))


def _extends(path: Path) -> str | None:
    match = re.search(r'{%\s*extends\s+"([^"]+)"', path.read_text()[:400])
    return match.group(1) if match else None


def _includes(path: Path) -> set[str]:
    return set(re.findall(r'{%\s*include\s+"([^"]+)"', path.read_text()))


def _partials_under_both_bases() -> set[str]:
    """Partials reachable from a page of each base layout.

    One level of include, which is what the tree actually has. A partial that only ever appears
    under one base is free to use that base's own stylesheet.
    """
    by_base: dict[str, set[str]] = {base: set() for base in BASES}
    for page in TEMPLATES.rglob("*.html"):
        base = _extends(page)
        if base not in by_base:
            continue
        pending = list(_includes(page))
        seen = set()
        while pending:
            name = pending.pop()
            if name in seen:
                continue
            seen.add(name)
            by_base[base].add(name)
            child = TEMPLATES / name
            if child.exists():
                pending.extend(_includes(child))
    return by_base["base_public.html"] & by_base["base_admin.html"]


def _class_names(body: str) -> set[str]:
    """Static class names in a template, skipping any built by a template expression."""
    names: set[str] = set()
    for value in re.findall(r'class="([^"]*)"', body):
        if "{" in value:
            continue
        names.update(value.split())
    return names


def _selectors(css: str) -> set[str]:
    """Class names a stylesheet writes rules for, comments stripped."""
    return set(re.findall(r"\.([A-Za-z][\w-]*)", re.sub(r"/\*.*?\*/", "", css, flags=re.S)))


# The class namespaces this project *owns*, as opposed to framework classes it merely
# re-skins. The distinction cannot be inferred from the stylesheets: `.dimm-toggle` is ours and
# `.badge.bg-success` is Bootstrap's with our brand color on it, and both look like a rule we
# wrote. Only the first kind is a layout dependency - a partial that loses it renders wrong,
# where one that loses a recolor renders in Bootstrap's green.
#
# Add a namespace here when you invent one. Forgetting only narrows what this test covers.
OURS = (
    "lumina-", "dimm-", "reveal-", "bench-", "compare-", "combobox",
    "identity-override", "component-override", "category-picker", "badge-validation",
)


def test_shared_partials_only_rely_on_shared_css():
    """Per base, not per stylesheet: what matters is that each layout links *something* defining
    the class. `.lumina-flags` is written out in both the public and the admin sheet, which is a
    duplication worth removing one day and is not this bug."""
    reachable = {
        base: set().union(*(
            _selectors((STATIC_CSS / name).read_text())
            for name in _linked_stylesheets(base)
        ))
        for base in BASES
    }

    offenders: dict[str, dict[str, list[str]]] = {}
    for partial in sorted(_partials_under_both_bases()):
        path = TEMPLATES / partial
        if not path.exists():
            continue
        used = {
            name for name in _class_names(path.read_text())
            if name.startswith(OURS)
        }
        for base in BASES:
            missing = used - reachable[base]
            if missing:
                offenders.setdefault(partial, {})[base] = sorted(missing)

    assert not offenders, (
        "these partials render under both base layouts, and a layout is missing the CSS they "
        f"depend on: {offenders}"
    )


def test_the_shared_sheet_is_linked_by_both_bases():
    """Named directly, so deleting the link from one base is caught even if nothing currently
    depends on it - which is the state the tree was in before the memory toggle was written."""
    for base in BASES:
        assert "lumina-shared.css" in _linked_stylesheets(base), base


def test_the_memory_toggle_is_hidden_wherever_it_renders():
    """The reported control, specifically. It is a checkbox whose label is the visible affordance,
    so an unstyled one is a mystery box next to a heading."""
    shared = "".join(
        (STATIC_CSS / name).read_text()
        for name in set.intersection(*(_linked_stylesheets(base) for base in BASES))
    )

    assert ".dimm-toggle" in shared
    assert ".dimm-extra" in shared
