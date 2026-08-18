"""The CSS-only disclosure controls, checked by asking the browser what is on the screen.

These are the project's answer to a problem that had no good answer: a form field that appears when
somebody says "actually, this is not that machine", inside a page that already has a form, in a
codebase that does not want a JavaScript dependency for it. The mechanism is a hidden checkbox, a
label, and a sibling selector.

That makes them the one part of the interface with no server-side evidence at all. The fields are
in the HTML whether or not they are shown, so every assertion the existing suite can make about
them passes in both states. Whether the label reveals anything is a fact about a stylesheet being
linked and a selector still matching, and there is no way to learn it except by rendering.

Not hypothetical: the ``.reveal-*`` rules lived in a stylesheet only one of the two layouts loaded,
so the same partial worked on the submitter's page and silently did nothing on the reviewer's. The
static check that now guards the linking was written after somebody noticed by eye.
"""
from __future__ import annotations

import pytest

from tests.browser import checks

pytestmark = pytest.mark.browser


def test_every_disclosure_on_the_reviewers_page_works(
    page, visit, sign_in, reviewer, pending_run,
):
    """Two of them: the listing-assignment override and the matched component's correction boxes.

    This layout is the one that broke. ``.reveal-*`` lived in a stylesheet only the public base
    linked, so the same partial worked for the submitter and silently did nothing here.
    """
    sign_in(reviewer)
    visit("review:run_detail", pending_run.pk)

    checks.assert_every_disclosure_reveals(page, expected=2)


def test_the_identity_override_opens_on_the_submitters_page(
    page, visit, sign_in, submitter, published_system,
):
    """"This is not that machine", reported as a button that redirected away and blanked the form.

    Only offered when there is something to dispute, so the machine has to already be listed.
    """
    from tests.browser.fixtures import make_run

    run = make_run(submitter)
    sign_in(submitter)
    visit("results:propose_listing", run.uuid)

    assert page.locator(".identity-override").count() == 1, (
        "the override was not offered, so the run did not match the listing and this test would "
        "prove nothing"
    )
    fields = page.locator(".identity-override .reveal-fields").first
    assert not fields.is_visible()

    page.locator("label[for='id_identity_disputed']").click()

    assert fields.is_visible()


def test_the_label_swaps_when_it_opens(page, visit, sign_in, submitter, published_system):
    """``reveal-when-open`` and ``reveal-when-closed`` are a pair, and both showing is a label
    that reads two contradictory things at once."""
    from tests.browser.fixtures import make_run

    run = make_run(submitter)
    sign_in(submitter)
    visit("results:propose_listing", run.uuid)

    closed = page.locator(".identity-override .reveal-when-closed").first
    opened = page.locator(".identity-override .reveal-when-open").first
    assert closed.is_visible() and not opened.is_visible()

    page.locator("label[for='id_identity_disputed']").click()

    assert opened.is_visible() and not closed.is_visible()


def test_the_publish_date_is_folded_away_until_unreleased_is_ticked(
    page, visit, sign_in, submitter,
):
    """"Publish on or after" is meaningless unless the results are embargoed - and the two are
    coupled server-side (``RunListingProposalForm.clean``) - so it stays folded away until
    "Unreleased hardware" is ticked, using the same CSS-only reveal as the identity override."""
    from tests.browser.fixtures import make_run

    run = make_run(submitter)
    sign_in(submitter)
    visit("results:propose_listing", run.uuid)

    date = page.locator("#id_publish_requested_date")
    assert date.count() == 1, "the embargo section did not render, so this would prove nothing"
    assert not date.is_visible(), "the publish date should be folded away until unreleased is ticked"

    page.locator("#id_pre_release").check()

    assert date.is_visible()


def test_the_vendor_ownership_claim_fields_are_folded_until_asked_for(
    page, visit, sign_in, submitter,
):
    """Proposing a new vendor hides the ownership-verification inputs until "I represent this
    vendor" is ticked - the same CSS-only reveal, on the admin base (base_admin.html) this time,
    which links the stylesheet just like the public one."""
    sign_in(submitter)
    visit("vendors:propose_new")

    work_email = page.locator("#id_work_email")
    assert work_email.count() == 1
    assert not work_email.is_visible(), "claim fields should be folded until ownership is ticked"

    page.locator("#id_claim_ownership").check()

    assert work_email.is_visible()


def test_the_dimm_table_folds(page, visit, sign_in, reviewer, pending_run):
    """Beyond four modules the rest collapses, or a 24-DIMM server pushes the run off screen."""
    sign_in(reviewer)
    visit("review:run_detail", pending_run.pk)

    extra = page.locator(".dimm-extra")
    assert extra.count() > 0, "this run reports four modules or fewer, so nothing folds"
    assert not extra.first.is_visible(), "rows beyond the fourth should start folded away"

    page.locator("label.dimm-more").first.click()

    assert extra.first.is_visible()
    assert page.locator(".dimm-less-text").first.is_visible(), (
        "the label should now offer to fold them back"
    )
