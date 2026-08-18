"""The review state machine, shared by every model a reviewer acts on.

Six models carried their own copy: hardware's ``Submission`` and
``ListingEditProposal``, software's ``SoftwareSubmission`` and
``SoftwareEditProposal``, and vendors' ``VendorProposal`` and ``VendorClaim``. The
four status constants were pasted six times, ``STATUS_CHOICES`` five, and the
``reject`` body was character-identical in all six once the guard on the line above
is set aside. Three of the six never declared ``OPEN_STATUSES``, which is why
``review/views.py`` re-derived that tuple by hand three times.

**A plain mixin, not an abstract Django model.** It declares no fields, so it cannot
emit a migration. Hoisting the columns was tempting - all six have identical
``status``, ``reviewer_notes``, and ``reviewed_at`` definitions - but their
``reviewed_by`` FKs differ in ``related_name`` (``reviewed_submissions``,
``reviewed_vendor_claims``, ...). An abstract base would have had to rewrite those
with ``%(class)s``, changing every reverse accessor that views and templates use by
name, for no gain beyond four fewer field declarations.

``approve()`` stays on each model. All six do genuinely different work: hardware's
mints listings and attestations, software's branches on tier between two evidence
tables, vendors' copies proposed fields onto a live record. Only the *rejection* half
of the machine is common, which is exactly the half that was duplicated.
"""
from __future__ import annotations

from django.utils import timezone


def stamp_review_decision(obj, *, status: str, by, reason: str) -> None:
    """Write a review outcome onto ``obj`` and persist just those four columns.

    The five lines every reviewer decision shares. ``ReviewWorkflow._record_decision`` uses it for the
    six submission/proposal/claim models; ``results.services`` uses it directly for ``TestRun`` -
    which deliberately does *not* inherit the mixin, because a run's review is a richer, service-driven
    state machine (draft, a quarantine that is itself rejectable, publish, attestation side effects)
    that the mixin's ``reject``/``request_changes`` methods would model wrongly. Duck-typed on the four
    review columns every reviewable carries.
    """
    obj.status = status
    obj.reviewer_notes = reason
    obj.reviewed_by = by
    obj.reviewed_at = timezone.now()
    obj.save(update_fields=["status", "reviewer_notes", "reviewed_by", "reviewed_at"])


class ReviewWorkflow:
    """Status vocabulary plus the two reviewer actions that are always the same.

    A subclass must declare ``status``, ``reviewer_notes``, ``reviewed_by``, and
    ``reviewed_at`` fields; the mixin only writes them.

    Set ``review_noun`` to whatever the thing is called in an error message
    ("submission", "proposal", "claim"), so a refusal reads in the subclass's own
    vocabulary rather than a generic one.
    """

    STATUS_PENDING = "pending"
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"
    STATUS_NEEDS_CHANGES = "needs-changes"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_REJECTED, "Rejected"),
        (STATUS_NEEDS_CHANGES, "Needs changes"),
    ]
    # Reviewable now: waiting on a reviewer, or sent back and resubmitted. Three of
    # the six models never declared this, so the review queue re-derived it inline.
    OPEN_STATUSES = (STATUS_PENDING, STATUS_NEEDS_CHANGES)

    # What a refusal calls this thing. Every subclass must set it: the default is
    # only a safety net, and leaving it in place produced "Cannot reject a submission"
    # for a *vendor claim*, which names a different object in this app entirely.
    # ``test_review_workflow.py`` pins one per model so that cannot recur silently.
    review_noun = "submission"

    # --- guards ---------------------------------------------------------------

    def _require_open(self, action: str) -> None:
        """Refuse ``action`` unless a reviewer can still act on this.

        The message quotes ``get_status_display()`` rather than the stored value: it is
        rendered straight into a reviewer's flash message (see
        ``review/views.py``'s ``except ValueError`` handlers), and "Approved" reads
        where "\'approved\'" is a developer convention that had leaked into the UI.
        Five of the six copies used the raw value and ``VendorClaim`` used the label;
        the label is the right end of that inconsistency to keep.

        No model overrides this. Hardware's ``Submission`` looked like it needed to -
        its guard was called ``_require_pending`` - but the body tested
        ``status != PENDING and status != NEEDS_CHANGES``, which is this rule exactly.
        The name claimed a stricter contract than the code kept.
        """
        if self.status not in self.OPEN_STATUSES:
            raise ValueError(
                f"Cannot {action} a {self.review_noun} with status "
                f"{self.get_status_display()!r}."
            )

    # --- the two actions that are identical everywhere ------------------------

    def _record_decision(self, *, status: str, by, reason: str) -> None:
        """Stamp the outcome. The five lines every reject and request_changes shared.

        One ``save`` with an explicit ``update_fields``, so a decision cannot
        accidentally write a column some caller mutated in memory beforehand.
        """
        stamp_review_decision(self, status=status, by=by, reason=reason)

    def _record_approval(self, *, by) -> None:
        """Stamp an approval: ``APPROVED`` plus reviewer and time, leaving ``reviewer_notes`` alone.

        The approve counterpart of ``_record_decision``, and the three columns every ``approve``
        writes the same way (hardware's ``Submission``, software's ``SoftwareSubmission``, vendors'
        proposal). It writes three, not four, because approve keeps any note from an earlier
        needs-changes round rather than clearing it. Each model's ``approve`` still does its own
        app-specific work - minting listings, attestations, copying vendor fields - around this stamp.
        """
        self.status = self.STATUS_APPROVED
        self.reviewed_by = by
        self.reviewed_at = timezone.now()
        self.save(update_fields=["status", "reviewed_by", "reviewed_at"])

    def reject(self, *, by, reason: str = "") -> None:
        self._require_open("reject")
        self._record_decision(status=self.STATUS_REJECTED, by=by, reason=reason)

    def resubmit(self) -> None:
        """Send a revised submission back for review.

        The other half of ``request_changes``, and the half that was missing on
        hardware: a reviewer could bounce a hardware submission but the submitter had no
        way to return it, so "needs changes" was a request with no reply channel.

        The same row returns to pending rather than a second submission opening against
        the same listing. ``approve`` already accepts a needs-changes submission, so one
        row carrying one decision was always the intent, and a second row would put the
        same thing in the queue twice for a reviewer to reconcile by hand.

        ``reviewer_notes`` is deliberately kept. It is what the reviewer asked for and
        they are about to check whether it was done. The dashboard shows it only while
        the status is needs-changes, so it does not read as a live complaint against a
        row that is now pending again.

        Lifted from ``SoftwareSubmission`` unchanged, which is where it was written and
        where it was the only copy. Hardware needed the identical body, and a second
        copy of a state transition is how the two drift into disagreeing about what
        resubmitting means.
        """
        if self.status != self.STATUS_NEEDS_CHANGES:
            raise ValueError(
                f"Only a {self.review_noun} a reviewer sent back can be resubmitted."
            )
        self.status = self.STATUS_PENDING
        # Cleared so the row reads as awaiting a decision rather than carrying the
        # previous one.
        self.reviewed_by = None
        self.reviewed_at = None
        self.save(update_fields=["status", "reviewed_by", "reviewed_at"])

    def request_changes(self, *, by, reason: str = "") -> None:
        """Send it back to its submitter.

        Pending-only in all five implementations that had this, including the ones
        whose ``reject`` accepted needs-changes: asking again for changes on something
        already awaiting them is a no-op that would reset the reviewer's note.
        """
        if self.status != self.STATUS_PENDING:
            raise ValueError(
                f"Cannot request changes on a {self.review_noun} with status "
                f"{self.get_status_display()!r}."
            )
        self._record_decision(
            status=self.STATUS_NEEDS_CHANGES, by=by, reason=reason
        )
