"""The reviewer's flow, driven in a browser.

Every one of these presses the button a person presses, rather than posting what we believe that
button posts. That distinction is the point: the review page's Approve control is attached to a
form defined in a different card by ``form="run-review"``, and if that association breaks the
click makes no request at all. No navigation, no error, no server-side trace, and every
server-side test still green because none of them go through the button.
"""
from __future__ import annotations

import pytest

from tests.browser import checks

pytestmark = pytest.mark.browser


def test_approving_a_run_actually_submits(page, visit, sign_in, reviewer, pending_run):
    """The click has to reach the server. This is the failure the shared form made possible."""
    sign_in(reviewer)
    visit("review:run_detail", pending_run.pk)

    page.get_by_role("button", name="Approve", exact=False).first.click()
    page.wait_for_url("**/review/")

    pending_run.refresh_from_db()
    assert pending_run.status == pending_run.STATUS_APPROVED
    assert page.get_by_text("Approved and published").is_visible()


def test_the_component_answers_travel_with_the_click(
    page, visit, sign_in, reviewer, pending_run,
):
    """Untick the vendor claim, press Approve, and the part must not be certified as Intel's.

    Server-side this is pinned by posting a payload built from the rendered page. Here the browser
    decides what to post, which is the only way to know the two agree.
    """
    from lumina.core.certification import ValidationLevel
    from lumina.hardware.models import CommunityAttestation

    sign_in(reviewer)
    visit("review:run_detail", pending_run.pk)

    claims = page.locator("input[name^='tie_claim']")
    assert claims.count() >= 1, "no vendor claim control was rendered"
    for index in range(claims.count()):
        box = claims.nth(index)
        assert box.is_checked(), "the premise: the claim is offered ticked"
        box.uncheck()

    page.get_by_role("button", name="Approve", exact=False).first.click()
    page.wait_for_url("**/review/")

    levels = {
        str(a.version.listing_component or a.version.listing_system): a.level
        for a in CommunityAttestation.objects.select_related("version")
    }
    assert levels, "nothing was attested, so this proves nothing"
    assert all(level == ValidationLevel.COMMUNITY for level in levels.values()), levels


def test_the_save_button_and_the_approve_button_go_to_different_places(
    page, visit, sign_in, reviewer, pending_run,
):
    """Two submit buttons in one form, distinguished only by ``formaction``. If that attribute is
    dropped, "Save component changes" and "Approve" become the same button and saving a
    correction silently approves the run."""
    sign_in(reviewer)
    visit("review:run_detail", pending_run.pk)

    page.get_by_role("button", name="Save component changes").click()
    page.wait_for_url(f"**/review/runs/{pending_run.pk}/")

    pending_run.refresh_from_db()
    assert pending_run.status == pending_run.STATUS_PENDING, "saving must not approve"
    assert page.get_by_text("Component ties updated").is_visible()


def test_every_named_control_belongs_to_a_form(page, visit, sign_in, reviewer, pending_run):
    sign_in(reviewer)
    visit("review:run_detail", pending_run.pk)

    checks.assert_named_controls_have_a_form(page)


def test_the_approve_button_is_in_the_component_form(
    page, visit, sign_in, reviewer, pending_run,
):
    """Not merely in *a* form. In the one carrying the component answers."""
    sign_in(reviewer)
    visit("review:run_detail", pending_run.pk)

    owner = page.evaluate("""() => {
        const buttons = [...document.querySelectorAll('button')];
        const approve = buttons.find(b => b.textContent.trim().startsWith('Approve')
                                          && !b.textContent.includes('all'));
        return approve ? (approve.form && approve.form.id) : 'no approve button';
    }""")

    assert owner == "run-review"

    carried = page.evaluate("""() => {
        const form = document.getElementById('run-review');
        return [...new FormData(form).keys()];
    }""")
    assert any(name.startswith("tie_claim") for name in carried), carried
    assert "components_submitted" in carried, carried
    assert "notes" in carried, "the decision note has to ride along too"


def test_nothing_on_the_review_page_sticks_out_sideways(
    page, visit, sign_in, reviewer, pending_run,
):
    """Found a real one on its first run: the badge column of each component row was
    ``flex-shrink-0``, so "and a new vendor, Dell Inc." pushed 27px past the viewport on any run
    whose parts are all new to the catalog."""
    sign_in(reviewer)
    visit("review:run_detail", pending_run.pk)

    checks.assert_no_horizontal_overflow(page)


def test_the_review_page_icons_have_glyphs(page, visit, sign_in, reviewer, pending_run):
    """The two layouts load different icon fonts, and the wrong one is a blank of exactly the
    size a designer might have chosen."""
    sign_in(reviewer)
    visit("review:run_detail", pending_run.pk)

    checks.assert_icons_render(page, minimum_elements=3)
