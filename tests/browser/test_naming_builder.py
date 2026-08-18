"""The naming-rule builder, driven in a browser.

This page is mostly JavaScript: condition rows are added and removed on the client, picking a
field fills in what the machine reported for it, and Preview posts the half-written form and
reports what it would do. None of that is reachable from a response body assertion - the markup
can be perfect while the button is attached to nothing - and all of it is what makes the form
usable instead of a text box asking for JSON.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser

FIELD = "input[name='condition_field']"
VALUE = "input[name='condition_value']"


# The card the standard fixture machine reports, an NVIDIA L40S.
VENDOR_ID, DEVICE_ID = "10de", "26b9"


def _open(visit, run, **params):
    return visit("review:naming_rule_new", kind="gpu", run=str(run.uuid), **params)


def _open_on_the_card(visit, run):
    return _open(visit, run, vendor_id=VENDOR_ID, device_id=DEVICE_ID)


def test_a_reviewer_can_add_a_condition_row(page, visit, sign_in, reviewer, pending_run):
    sign_in(reviewer)
    _open(visit, pending_run)
    before = page.locator(f"#conditions {FIELD}").count()

    page.get_by_role("button", name="Add a condition").click()

    assert page.locator(f"#conditions {FIELD}").count() == before + 1


def test_an_added_row_comes_up_empty(page, visit, sign_in, reviewer, pending_run):
    """It is cloned from a prefilled row, so without clearing it arrives holding a duplicate of
    the condition above it."""
    sign_in(reviewer)
    _open_on_the_card(visit, pending_run)

    page.get_by_role("button", name="Add a condition").click()
    added = page.locator("#conditions .condition-row").last

    assert added.locator(FIELD).input_value() == ""
    assert added.locator(VALUE).input_value() == ""


def test_a_reviewer_can_drop_a_row(page, visit, sign_in, reviewer, pending_run):
    """Widening a rule means deleting a condition, which is the commonest edit there is."""
    sign_in(reviewer)
    _open_on_the_card(visit, pending_run)
    rows = page.locator("#conditions .condition-row")
    assert rows.count() == 2

    rows.last.get_by_role("button").click()

    assert page.locator("#conditions .condition-row").count() == 1


def test_picking_a_field_fills_in_what_this_machine_says(
    page, visit, sign_in, reviewer, pending_run,
):
    """The reason the field list carries values at all. Typing a path and then copying its value
    out of a table by hand is the work this page exists to remove."""
    sign_in(reviewer)
    _open_on_the_card(visit, pending_run)
    page.get_by_role("button", name="Add a condition").click()
    row = page.locator("#conditions .condition-row").last

    row.locator(FIELD).fill("pci.vendor_id")
    row.locator(FIELD).dispatch_event("change")

    assert row.locator(VALUE).input_value() == VENDOR_ID


def test_a_value_already_typed_is_not_overwritten(page, visit, sign_in, reviewer, pending_run):
    """Autofill is a convenience, and one that discards what somebody typed is not."""
    sign_in(reviewer)
    _open_on_the_card(visit, pending_run)
    page.get_by_role("button", name="Add a condition").click()
    row = page.locator("#conditions .condition-row").last
    row.locator(VALUE).fill("mine")

    row.locator(FIELD).fill("pci.vendor_id")
    row.locator(FIELD).dispatch_event("change")

    assert row.locator(VALUE).input_value() == "mine"


def test_preview_reports_what_the_rule_would_do(page, visit, sign_in, reviewer, pending_run):
    """The two questions somebody writing a rule has, answered without saving anything: what does
    this call the part in front of me, and how much else does it touch."""
    sign_in(reviewer)
    _open_on_the_card(visit, pending_run)
    page.locator("input[name='build']").fill("Arc-era integrated")

    page.get_by_role("button", name="Preview").click()

    page.wait_for_function(
        "() => !document.getElementById('preview-out').textContent.includes('Checking')")
    assert "Arc-era integrated" in page.locator("#preview-out").inner_text()


def test_preview_reports_a_rule_it_refuses(page, visit, sign_in, reviewer, pending_run):
    """A template error has to arrive here rather than as a rejected save, because the point of
    previewing is finding out before committing to anything."""
    sign_in(reviewer)
    _open(visit, pending_run)
    page.locator("input[name='build']").fill("{{ unclosed ")

    page.get_by_role("button", name="Preview").click()

    page.wait_for_function(
        "() => !document.getElementById('preview-out').textContent.includes('Checking')")
    assert "build:" in page.locator("#preview-out").inner_text()


def test_the_rows_a_reviewer_built_are_what_gets_saved(
    page, visit, sign_in, reviewer, pending_run,
):
    """End to end through the interface, which is the only way to know the client-side rows and
    the server-side reader agree about the shape they are posted in."""
    from lumina.hardware.models import ComponentNamingRule

    sign_in(reviewer)
    _open_on_the_card(visit, pending_run)
    page.locator("input[name='build']").fill("Arc-era integrated")
    page.locator("input[name='priority']").fill("7")

    page.get_by_role("button", name="Save rule").click()
    page.wait_for_url("**/review/naming/")

    rule = ComponentNamingRule.objects.get(priority=7)
    assert rule.build == "Arc-era integrated"
    assert rule.conditions == [
        {"field": "pci.vendor_id", "op": "eq", "value": VENDOR_ID},
        {"field": "pci.device_id", "op": "eq", "value": DEVICE_ID},
    ]


# --- finding a field you do not already know the name of ---------------------------
#
# The fields were offered only through a <datalist>, which suggests nothing until you type the
# start of a path. A reviewer who did not already know "pci.vendor_id" existed had no way to find
# out it did, and the name template had no suggestion at all.

PICK = ".field-pick:not([hidden])"


def test_the_fields_are_listed_without_typing_anything(
    page, visit, sign_in, reviewer, pending_run,
):
    sign_in(reviewer)
    _open_on_the_card(visit, pending_run)

    assert page.locator(PICK).count() > 10
    assert page.get_by_text("pci.vendor_id", exact=True).is_visible()


def test_a_field_shows_the_value_beside_it(page, visit, sign_in, reviewer, pending_run):
    """Which is what makes the list a way to decide, not just a way to spell."""
    sign_in(reviewer)
    _open_on_the_card(visit, pending_run)

    row = page.locator("[data-path='pci.vendor_id']")

    assert VENDOR_ID in row.inner_text()


def test_filtering_narrows_the_list_to_a_prefix(page, visit, sign_in, reviewer, pending_run):
    """"What is there under pci" is the question, and the answer was a list of everything."""
    sign_in(reviewer)
    _open_on_the_card(visit, pending_run)

    page.locator("#field-filter").fill("pci.")

    paths = page.locator(PICK).all_text_contents()
    assert paths and all("pci." in text for text in paths)


def test_a_group_with_nothing_left_in_it_is_hidden(page, visit, sign_in, reviewer, pending_run):
    """A heading with no fields under it reads as a group that has none."""
    sign_in(reviewer)
    _open_on_the_card(visit, pending_run)
    assert page.locator(".field-group:not([hidden])").count() > 1

    page.locator("#field-filter").fill("pci.")

    assert page.locator(".field-group:not([hidden])").count() == 1


def test_clicking_a_field_fills_the_condition_being_edited(
    page, visit, sign_in, reviewer, pending_run,
):
    sign_in(reviewer)
    _open_on_the_card(visit, pending_run)
    page.get_by_role("button", name="Add a condition").click()
    row = page.locator("#conditions .condition-row").last
    row.locator(FIELD).click()

    page.locator("[data-path='pci.driver']").click()

    assert row.locator(FIELD).input_value() == "pci.driver"
    assert row.locator(VALUE).input_value() == "nvidia"


def test_clicking_a_field_writes_it_into_the_name_template(
    page, visit, sign_in, reviewer, pending_run,
):
    """The half that had no suggestion at all, and the half a reviewer is on the page to write."""
    sign_in(reviewer)
    _open_on_the_card(visit, pending_run)
    build = page.locator("input[name='build']")
    build.fill("")
    build.click()

    page.locator("[data-path='cpu.model']").click()

    assert build.input_value() == "{{ cpu.model }}"


def test_a_field_is_inserted_at_the_cursor_not_appended(
    page, visit, sign_in, reviewer, pending_run,
):
    """Writing "X (Y)" means putting the second field inside brackets that are already typed."""
    sign_in(reviewer)
    _open_on_the_card(visit, pending_run)
    build = page.locator("input[name='build']")
    build.fill("Card: ()")
    build.click()
    page.evaluate(
        "() => { const i = document.querySelector(\"[name='build']\");"
        " i.setSelectionRange(7, 7); }")

    page.locator("[data-path='model']").click()

    assert build.input_value() == "Card: ({{ model }})"


def test_a_click_with_nothing_focused_goes_to_the_name(
    page, visit, sign_in, reviewer, pending_run,
):
    """The field somebody lands on this page to write, so it is the least surprising default.

    Nothing is touched first, deliberately: filling an input to set it up focuses it, which is
    the very state this is meant to be testing the absence of.
    """
    sign_in(reviewer)
    _open_on_the_card(visit, pending_run)
    was = page.locator("input[name='build']").input_value()

    page.locator("[data-path='vendor']").click()

    assert page.locator("input[name='build']").input_value() == was + "{{ vendor }}"


def test_the_expression_a_click_inserts_is_one_the_rule_accepts(
    page, visit, sign_in, reviewer, pending_run,
):
    """End to end: click a field into the name, save, and the rule renders that machine's value.
    A suggestion that does not survive the round trip is worse than no suggestion."""
    from lumina.hardware.models import ComponentNamingRule
    from lumina.results import naming

    sign_in(reviewer)
    _open_on_the_card(visit, pending_run)
    build = page.locator("input[name='build']")
    build.fill("")
    build.click()
    page.locator("[data-path='pci.driver']").click()
    page.locator("input[name='priority']").fill("7")

    page.get_by_role("button", name="Save rule").click()
    page.wait_for_url("**/review/naming/")

    rule = ComponentNamingRule.objects.get(priority=7)
    assert naming.render(rule.build, {"pci": {"driver": "nvidia"}}) == "nvidia"


# --- going back to a rule ----------------------------------------------------------
#
# Rules could be written and never changed. The one that needs changing is the one that turned out
# too broad, and the only way to reach it was the Django admin, which a reviewer cannot open.


@pytest.fixture
def saved_rule(db):
    from lumina.hardware.models import ComponentNamingRule

    return ComponentNamingRule.objects.create(
        kind="gpu", priority=7, build="Arc-era integrated", notes="from a review",
        conditions=[{"field": "pci.vendor_id", "op": "eq", "value": VENDOR_ID}])


def test_a_rule_can_be_reached_from_the_plan_page(page, visit, sign_in, reviewer, saved_rule):
    sign_in(reviewer)
    visit("review:naming_plan")

    page.get_by_role("link", name="Edit").first.click()

    page.wait_for_url(f"**/naming/{saved_rule.pk}/")
    assert page.locator("input[name='build']").input_value() == "Arc-era integrated"


def test_editing_a_rule_saves_over_it(page, visit, sign_in, reviewer, saved_rule):
    sign_in(reviewer)
    visit("review:naming_rule_edit", saved_rule.pk)
    page.locator("input[name='build']").fill("Intel Arc 140V")

    page.get_by_role("button", name="Save rule").click()
    page.wait_for_url("**/review/naming/")

    saved_rule.refresh_from_db()
    assert saved_rule.build == "Intel Arc 140V"


def test_the_conditions_come_back_editable(page, visit, sign_in, reviewer, saved_rule):
    """The prefill that matters: narrowing or widening a match is the edit somebody makes."""
    sign_in(reviewer)
    visit("review:naming_rule_edit", saved_rule.pk)
    row = page.locator("#conditions .condition-row").first

    assert row.locator(FIELD).input_value() == "pci.vendor_id"
    assert row.locator(VALUE).input_value() == VENDOR_ID


def test_unticking_enabled_turns_the_rule_off(page, visit, sign_in, reviewer, saved_rule):
    """The reversible stop, and the reason deleting is tucked away below it."""
    sign_in(reviewer)
    visit("review:naming_rule_edit", saved_rule.pk)

    page.locator("#enabled").uncheck()
    page.get_by_role("button", name="Save rule").click()
    page.wait_for_url("**/review/naming/")

    saved_rule.refresh_from_db()
    assert saved_rule.enabled is False


def test_a_rule_can_be_deleted_from_the_form(page, visit, sign_in, reviewer, saved_rule):
    from lumina.hardware.models import ComponentNamingRule

    sign_in(reviewer)
    visit("review:naming_rule_edit", saved_rule.pk)

    page.get_by_text("Delete this rule").click()
    page.get_by_role("button", name="Delete permanently").click()
    page.wait_for_url("**/review/naming/")

    assert not ComponentNamingRule.objects.filter(pk=saved_rule.pk).exists()


def test_the_run_page_says_a_rule_renamed_a_part(page, visit, sign_in, reviewer, pending_run):
    """The question this answers on a review page: is this name the machine's, or ours?"""
    from lumina.hardware.models import ComponentNamingRule

    rule = ComponentNamingRule.objects.create(
        kind="gpu", priority=1, build="Renamed by a rule",
        conditions=[{"field": "pci.vendor_id", "op": "eq", "value": VENDOR_ID}])
    sign_in(reviewer)
    visit("review:run_detail", pending_run.pk)

    badge = page.get_by_role("link", name="renamed by rule").first

    assert badge.is_visible()
    assert f"/naming/{rule.pk}/" in badge.get_attribute("href")


def test_previewing_a_saved_rule_works(page, visit, sign_in, reviewer, saved_rule):
    """Preview refused every rule that was already saved: the throwaway rule it builds had no id,
    so the ambiguity check found the rule being edited and called it a duplicate."""
    sign_in(reviewer)
    visit("review:naming_rule_edit", saved_rule.pk)

    page.get_by_role("button", name="Preview").click()
    page.wait_for_function(
        "() => !document.getElementById('preview-out').textContent.includes('Checking')")

    assert "priority:" not in page.locator("#preview-out").inner_text()


def test_the_cheatsheet_sits_beside_the_fields(page, visit, sign_in, reviewer, pending_run):
    """Two halves of one question. The field list says what there is to match on and says nothing
    about how to match it, and the pattern is where somebody who is not a regular expression
    person gets stuck."""
    sign_in(reviewer)
    _open_on_the_card(visit, pending_run)

    fields = page.locator("#field-picker-heading").bounding_box()
    cheatsheet = page.get_by_text("Writing the pattern").bounding_box()

    assert cheatsheet is not None, "the cheatsheet did not render"
    assert cheatsheet["y"] < fields["y"] + fields["height"] + 400, "it is nowhere near the fields"


def test_the_page_does_not_scroll_sideways_with_both_cards(
    page, visit, sign_in, reviewer, pending_run,
):
    """Two cards in a row and a table of patterns in one of them is exactly how a page starts
    overflowing at the widths people actually use."""
    from tests.browser import checks

    sign_in(reviewer)
    _open_on_the_card(visit, pending_run)

    checks.assert_no_horizontal_overflow(page)


def test_the_cheatsheet_survives_a_phone_width(page, visit, sign_in, reviewer, pending_run):
    from tests.browser import checks

    sign_in(reviewer)
    page.set_viewport_size({"width": 400, "height": 900})
    _open_on_the_card(visit, pending_run)

    assert page.get_by_text("Writing the pattern").is_visible()
    checks.assert_no_horizontal_overflow(page)


# --- finding a field by what it says, and knowing the wildcard exists ---------------


def test_the_two_filters_narrow_on_different_things(
    page, visit, sign_in, reviewer, pending_run,
):
    """"What is there under pci" and "where does this machine say 10de" are different questions,
    and one box matching either meant a value search for "1" hit every indexed path."""
    sign_in(reviewer)
    _open_on_the_card(visit, pending_run)

    page.locator("#value-filter").fill(VENDOR_ID)

    paths = page.locator(PICK).all_text_contents()
    assert paths and all(VENDOR_ID in text for text in paths)


def test_the_filters_narrow_together(page, visit, sign_in, reviewer, pending_run):
    sign_in(reviewer)
    _open_on_the_card(visit, pending_run)

    page.locator("#field-filter").fill("cpu.")
    page.locator("#value-filter").fill(VENDOR_ID)

    assert page.locator(PICK).count() == 0, "no cpu field holds the GPU's vendor id"


def test_a_value_search_finds_a_field_whose_name_says_nothing(
    page, visit, sign_in, reviewer, pending_run,
):
    """The reason this box exists: somebody who can see a string on their screen and cannot guess
    which of two hundred paths holds it."""
    sign_in(reviewer)
    _open_on_the_card(visit, pending_run)

    page.locator("#value-filter").fill("NVIDIA L40S")

    assert page.locator(PICK).count() >= 1


def test_clicking_a_field_into_the_pattern_field_offers_the_wildcard(
    page, visit, sign_in, reviewer, vendor_engineer,
):
    """A reader who only ever sees ``dmi.processor.0.Part Number`` has no way to know the other
    form exists, and the indexed one is the wrong answer on a two-socket machine."""
    from lumina.results import ingest
    from lumina.results.tests import factories as f

    inventory = f.default_inventory()
    inventory["summary"]["fields"] = {
        "dmi": {"processor": [{"props": {"Part Number": "Q80-30"}}]},
    }
    run = ingest.ingest_bundle(
        submitter=vendor_engineer, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"], inventory=inventory,
            results=[f.validate_result("validate.cpu.functional")]))))
    sign_in(reviewer)
    visit("review:naming_rule_new", kind="cpu", run=str(run.uuid))
    page.locator("input[name='match_field']").click()

    page.locator("[data-path='dmi.processor.0.Part Number']").click()

    assert page.locator("input[name='match_field']").input_value() == (
        "dmi.processor.*.Part Number")


def test_a_field_with_no_index_goes_in_unchanged(
    page, visit, sign_in, reviewer, pending_run,
):
    """There is nothing to widen, and a wildcard where no list exists would match nothing."""
    sign_in(reviewer)
    _open_on_the_card(visit, pending_run)
    page.locator("input[name='match_field']").click()

    page.locator("[data-path='pci.driver']").click()

    assert page.locator("input[name='match_field']").input_value() == "pci.driver"


def test_the_page_says_what_a_star_does(page, visit, sign_in, reviewer, pending_run):
    sign_in(reviewer)
    _open_on_the_card(visit, pending_run)

    assert page.get_by_text("stands for any list index").is_visible()


def test_the_field_box_does_not_match_values(page, visit, sign_in, reviewer, pending_run):
    """Two boxes only earn their place by searching different things. One box matching either is
    how a search for "1" hits every path with a list index in it."""
    sign_in(reviewer)
    _open_on_the_card(visit, pending_run)

    page.locator("#field-filter").fill("NVIDIA L40S")

    assert page.locator(PICK).count() == 0, "that string is a value, not a path"
