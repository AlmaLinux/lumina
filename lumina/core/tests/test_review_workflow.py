"""The shared review state machine, and the thing that made sharing it risky.

``ReviewWorkflow`` replaced six copies of the same ``reject``/``request_changes``
pair. The copies were character-identical, so the refactor was safe in every respect
but one: each carried its **own noun** in its refusal message, and a mixin has to be
told. Two models were missed, and every reviewer-facing refusal on a vendor claim or
vendor proposal started calling it a "submission" - which in this app is a different
object entirely.

Nothing caught it. The suite's own assertions were `pytest.raises(ValueError)` with no
``match=``, so the wording was unpinned across all six models and 1281 tests passed
with the text wrong. That is the gap this file closes: one assertion per model on the
noun it uses, so a seventh reviewable model cannot inherit the default silently.
"""
from __future__ import annotations

import pytest

from lumina.core.review import ReviewWorkflow

pytestmark = pytest.mark.django_db


# Every reviewable model and the word its refusals must use. Adding a model here is
# the point: a new one that forgets ``review_noun`` fails at ``test_every_...``.
EXPECTED_NOUNS = {
    "hardware.Submission": "submission",
    "hardware.ListingEditProposal": "listing edit",
    "software.SoftwareSubmission": "submission",
    "software.SoftwareEditProposal": "software edit proposal",
    "vendors.VendorProposal": "proposal",
    "vendors.VendorClaim": "claim",
    "survey.SurveyTokenRequest": "survey token request",
}


def _reviewable_models() -> dict:
    """Every concrete model using the mixin, keyed ``app.Model``."""
    from django.apps import apps

    return {
        f"{m._meta.app_label}.{m.__name__}": m
        for m in apps.get_models()
        if issubclass(m, ReviewWorkflow)
    }


def test_the_set_of_reviewable_models_is_what_we_think_it_is():
    """If this fails, a model gained or lost the mixin and the table below is stale."""
    assert set(_reviewable_models()) == set(EXPECTED_NOUNS)


@pytest.mark.parametrize("label,noun", sorted(EXPECTED_NOUNS.items()))
def test_each_model_names_itself_in_its_refusals(label, noun):
    """The regression, pinned per model.

    ``VendorProposal`` and ``VendorClaim`` inherited the default and called themselves
    submissions.
    """
    model = _reviewable_models()[label]

    assert model.review_noun == noun


def test_no_model_relies_on_the_inherited_default_by_accident():
    """The default exists as a safety net, not as a value to leave in place.

    Only the two genuine submissions may match it, and they say so explicitly on the
    class rather than inheriting it - so a model that simply forgot is distinguishable
    from one that meant it.
    """
    declared_locally = {
        label for label, model in _reviewable_models().items()
        if "review_noun" in vars(model)
    }

    assert declared_locally == set(EXPECTED_NOUNS), (
        "these models inherit review_noun instead of declaring it: "
        f"{sorted(set(EXPECTED_NOUNS) - declared_locally)}"
    )


def test_the_refusal_quotes_a_human_label_not_a_stored_value():
    """It goes straight into a reviewer's flash message via ``messages.error``.

    Five of the six copies interpolated the raw column value and ``VendorClaim`` used
    the display label; the mixin keeps the label.
    """
    from lumina.vendors.models import VendorClaim

    claim = VendorClaim(status=VendorClaim.STATUS_APPROVED)

    with pytest.raises(ValueError) as exc:
        claim.reject(by=None, reason="")

    message = str(exc.value)
    assert "a claim" in message, message
    assert "'Approved'" in message, message
    assert "approved'" not in message.replace("'Approved'", ""), (
        f"the raw stored value leaked into a user-facing message: {message}"
    )


def test_request_changes_refuses_the_same_way():
    from lumina.vendors.models import VendorProposal

    proposal = VendorProposal(status=VendorProposal.STATUS_REJECTED)

    with pytest.raises(ValueError) as exc:
        proposal.request_changes(by=None, reason="")

    assert "a proposal" in str(exc.value)
    assert "'Rejected'" in str(exc.value)


def test_softwarecompatibility_is_not_reviewable():
    """It has a status column and deliberately does not use this machine.

    Two values, not four: a cited major is pending or approved. There is no rejected
    (rejecting deletes the row) and no needs-changes (a major number has nothing for a
    submitter to revise). A blanket application of the mixin swept it in during the
    refactor and ``makemigrations --check`` caught the altered choices.
    """
    from lumina.software.models import SoftwareCompatibility

    assert not issubclass(SoftwareCompatibility, ReviewWorkflow)
    assert [c[0] for c in SoftwareCompatibility.STATUS_CHOICES] == [
        "pending", "approved",
    ]
    assert not hasattr(SoftwareCompatibility, "reject")
