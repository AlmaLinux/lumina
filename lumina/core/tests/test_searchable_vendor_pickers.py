"""The publisher/vendor pickers are searchable lists, not native dropdowns.

Both submit forms offered every published vendor as a plain ``<select>``. That is fine
at twenty vendors and unusable at two thousand: a native dropdown means scrolling and
the browser's prefix-only type-ahead, so a submitter looking for "Hewlett Packard
Enterprise" cannot find it by typing "HPE".

The catalog already had the answer - ``combobox.js`` turns a select carrying
``data-combobox`` into a filter-as-you-type list, and the reviewer's listing-assign
form has used it all along. These forms just never opted in.

**The select stays.** It keeps the value, so server-side validation, ``clean_vendor``,
and the inline-vendor sentinel are untouched, and with JavaScript off the field is an
ordinary dropdown that still works. Only the picking changes.

The subtle part, and the reason this file exists: the menu caps at twelve entries and
"+ Propose a new vendor…" is the *last* option. An empty query preserves the server's
order, so past twelve vendors that entry fell off the bottom and the inline-vendor flow
became reachable only by guessing that typing "propose" found it. ``data-combobox-pin``
names it so it always survives the cap. See ``comboVisible`` and the pinning block in
``tests/js/combobox_check.js`` for the behaviour itself.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth.models import User

pytestmark = pytest.mark.django_db


def _forms():
    """The two submit forms whose vendor picker is an unbounded list."""
    from lumina.hardware.forms import SubmissionForm
    from lumina.software.forms import SoftwareSubmissionForm

    user = User.objects.create_user("picker-probe")
    return {
        "software publisher": SoftwareSubmissionForm(user=user)["vendor"],
        "hardware vendor": SubmissionForm(user=user)["vendor"],
    }


@pytest.mark.parametrize("label", ["software publisher", "hardware vendor"])
def test_the_picker_is_searchable(label):
    field = _forms()[label]

    assert field.field.widget.attrs.get("data-combobox") == "true"


@pytest.mark.parametrize("label", ["software publisher", "hardware vendor"])
def test_the_picker_stays_a_select(label):
    """Not a text input. A publisher has to be one that exists, so the field stays a
    strict choice and only the picking gets easier - and the value survives with
    JavaScript off."""
    from django import forms

    field = _forms()[label]

    assert isinstance(field.field.widget, forms.Select)
    assert "<select" in str(field)


@pytest.mark.parametrize("label", ["software publisher", "hardware vendor"])
def test_the_propose_a_vendor_option_is_pinned(label):
    """The escape hatch must not be truncated by the twelve-entry cap.

    A list of ordinary choices being capped is fine - that is what searching is for.
    An action being capped means nobody finds it.
    """
    from lumina.hardware.forms import INLINE_VENDOR_SENTINEL

    field = _forms()[label]

    assert field.field.widget.attrs.get("data-combobox-pin") == INLINE_VENDOR_SENTINEL


@pytest.mark.parametrize("label", ["software publisher", "hardware vendor"])
def test_the_search_box_says_what_it_searches(label):
    field = _forms()[label]
    placeholder = field.field.widget.attrs.get("data-placeholder", "")

    assert placeholder.startswith("Search "), placeholder


def test_the_sentinel_is_still_the_last_option():
    """Pinning renders it last too, so the two orders agree. If the form ever put it
    first, the pinned copy would appear at the bottom and the unpinned one at the top.
    """
    from lumina.software.forms import SoftwareSubmissionForm
    from lumina.vendors.models import Vendor

    Vendor.objects.create(name="Zebra Publishing", published=True,
                          scope=Vendor.SCOPE_SOFTWARE)
    user = User.objects.create_user("order-probe")
    choices = list(SoftwareSubmissionForm(user=user).fields["vendor"].choices)

    assert choices[-1][0] == "__new__", choices[-1]


def test_a_vendor_with_no_javascript_can_still_be_chosen():
    """The whole point of keeping the select: the form must validate a posted value
    with no client-side help at all."""
    from lumina.software.forms import SoftwareSubmissionForm
    from lumina.vendors.models import Vendor

    vendor = Vendor.objects.create(name="Vaultwise", published=True,
                                   scope=Vendor.SCOPE_SOFTWARE)
    user = User.objects.create_user("nojs-probe")
    form = SoftwareSubmissionForm(user=user)

    assert vendor.slug in [value for value, _ in form.fields["vendor"].choices]


def test_the_combobox_javascript_checks_pass():
    """Runs ``tests/js/combobox_check.js``, which nothing else did.

    The ranking and the pinning are the whole value of the widget, and that script was
    the only thing testing them - as a standalone ``node`` invocation wired into
    neither pytest nor CI, so in practice it ran when somebody remembered. Skipped
    rather than failed where node is absent, so this does not make the Python suite
    depend on a JS runtime being installed.
    """
    # ``shutil.which``, not a trial ``subprocess.run``: a missing executable makes
    # ``run`` raise FileNotFoundError rather than return non-zero, and ``check=False``
    # does not cover that. The first version of this guard checked the return code and
    # so blew up instead of skipping in the CI container, which has no node.
    if shutil.which("node") is None:
        pytest.skip("node is not available in this environment")

    script = Path(settings.BASE_DIR) / "tests" / "js" / "combobox_check.js"
    result = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


# --- making the control legible ------------------------------------------------
#
# The first version of this feature drew a correct search box that nobody could use.
# Two reports, both fair:
#
#   "it's not clear that you can type in the box"
#   "it's also not clear what to do to create a new publisher"
#
# The first had a specific cause: ``setupSelect`` copied the select's class onto the
# input, so the box inherited Bootstrap's ``.form-select`` - caret, padding and all -
# and looked exactly like something you click rather than type in. ``comboInputClass``
# translates it now, and the node check covers that.
#
# The second was a flow problem. Picking "+ Add a new publisher" set a hidden value and
# closed the menu; the fields it unlocked sat in a card further down, whose only
# explanation was a sentence inside that card telling you to have already done this. So:
# help text where the decision is made, a label in the field's own vocabulary, and
# ``data-combobox-pin-target`` to scroll and focus what the action opens.


@pytest.mark.parametrize("label,noun", [
    ("software publisher", "publisher"), ("hardware vendor", "vendor"),
])
def test_the_add_option_uses_the_fields_own_vocabulary(label, noun):
    """The software field is labelled "Publisher" and offered "+ Propose a new
    *vendor*"; hardware's said "(inline)", which is implementation jargon."""
    field = _forms()[label]
    sentinel = dict(field.field.choices)["__new__"]

    assert sentinel == f"+ Add a new {noun}…", sentinel


@pytest.mark.parametrize("label", ["software publisher", "hardware vendor"])
def test_choosing_the_add_option_goes_somewhere(label):
    """Otherwise the action sets a hidden value and nothing visibly happens."""
    field = _forms()[label]

    assert field.field.widget.attrs.get("data-combobox-pin-target") == (
        "#id_new_vendor_name"
    )


@pytest.mark.parametrize("url_name,noun", [
    ("software:submit", "publisher"), ("submit:start", "vendor"),
])
def test_the_page_says_you_can_type_and_what_to_do(client, url_name, noun):
    """Help text at the point of decision, on both submit pages.

    The software page had none at all, and hardware's named the old option label, so
    following it meant looking for a control that no longer existed.
    """
    from django.urls import reverse

    user = User.objects.create_user(f"helptext-{noun}", password="pw")
    client.force_login(user)

    body = client.get(reverse(url_name)).content.decode()

    assert "Start typing to search" in body
    assert f"+ Add a new {noun}" in body


def test_the_focus_target_exists_on_the_page(client):
    """A stale selector makes ``followPin`` a silent no-op, which is the exact
    failure this feature was reported for."""
    import re

    from django.urls import reverse

    user = User.objects.create_user("target-probe", password="pw")
    client.force_login(user)
    body = client.get(reverse("software:submit")).content.decode()

    target = re.search(r'data-combobox-pin-target="#([^"]+)"', body)
    assert target, "no focus target rendered"
    assert f'id="{target.group(1)}"' in body, (
        f"the picker points at #{target.group(1)}, which is not on the page"
    )


# --- the control has to look and behave like a text box ------------------------
#
# Two follow-up reports after the first version shipped, and both were about the gap
# between "correct" and "visible":
#
#   "what if the cursor changes to a blinking cursor in the box indicating you can type?"
#   "I don't see the add new publisher option in the list at all even though it says
#    it's there"
#
# The second was a real defect and the tests above could not see it. ``comboVisible``
# keeps the action in the list past the twelve-entry cap - that part worked and was
# tested - but the menu is ``max-height: 16rem; overflow-y: auto``, roughly eight rows,
# so as the thirteenth entry the action sat below the scroll fold. Surviving the cap is
# not the same as being on screen.
#
# These assert the CSS directly, the way test_shared_presentation.py asserts the badge
# palette. It is not a substitute for looking at the page, but it pins the two
# properties whose absence caused the reports.

_COMBOBOX_CSS = Path(settings.BASE_DIR) / "static" / "css" / "combobox.css"


def _rule(selector: str) -> str:
    """The declarations of the first rule matching ``selector``."""
    import re

    css = _COMBOBOX_CSS.read_text()
    match = re.search(
        rf"{re.escape(selector)}\s*(?:,[^{{]*)?\{{(.*?)\}}", css, re.S
    )
    assert match, f"no rule for {selector}"
    return match.group(1)


def test_the_search_box_shows_a_text_cursor():
    """An I-beam on hover, a caret on click: the standard "you can type" signals.

    Stated explicitly rather than left to the default, because this element replaces a
    ``<select>`` and anything resembling a dropdown gets clicked at and waited on.
    """
    assert "cursor: text" in _rule(".combobox-input")


def test_the_add_option_stays_on_screen_while_the_list_scrolls():
    """The reported defect. The action is the last entry in a menu that scrolls at
    about eight rows, so without sticking it, reaching it means scrolling to the
    bottom - and nobody scrolls a list they are searching."""
    pinned = _rule(".combobox-menu .combobox-pinned")

    assert "position: sticky" in pinned
    assert "bottom: 0" in pinned


def test_the_sticky_option_is_opaque():
    """It scrolls over the rows beneath it. A transparent background shows two
    overlapping labels, which reads as a rendering bug."""
    assert "background-color:" in _rule(".combobox-menu .combobox-pinned")


def test_the_menu_really_is_the_scroll_container():
    """``position: sticky`` sticks to the nearest scrolling ancestor. If the overflow
    ever moves off ``.combobox-menu``, the rule above silently stops working."""
    menu = _rule(".combobox-menu")

    assert "overflow-y: auto" in menu
    assert "max-height" in menu


def test_the_pinned_class_is_applied_to_the_rendered_entry():
    """Source-level, because ``setupSelect`` needs a DOM: the CSS above is inert
    unless the class actually lands on the button."""
    source = (Path(settings.BASE_DIR) / "static" / "js" / "combobox.js").read_text()
    body = source[source.index("function setupSelect"):source.index("function setupPicker")]

    assert "combobox-pinned" in body
    assert "menu.appendChild(item)" in body, (
        "the entry must be a direct child of the scroll container for sticky to apply"
    )


def test_nothing_can_scroll_through_below_the_sticky_row():
    """The third report on this control, and the last of the sticky artifacts.

    ``position: sticky; bottom: 0`` pins to the scrollport's *padding* edge, and
    Bootstrap's ``.dropdown-menu`` carries 0.5rem of vertical padding - so the list
    kept scrolling through the 8px strip underneath the pinned row and a half-visible
    "Docker, Inc." appeared below "+ Add a new publisher…".

    Asserted on the two-class selector deliberately: a single ``.combobox-menu`` rule
    only wins by source order, and Bootstrap loads from a CDN.
    """
    assert "padding-bottom: 0" in _rule(".combobox-menu.dropdown-menu")
