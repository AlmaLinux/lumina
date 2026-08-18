"""The catalog of notifiable events: what each one is and who hears about it.

Adding an event is a row here plus one ``emit(...)`` call at the transition. Audiences are named
here and resolved from the live target at delivery time (see ``services._recipients``), reusing the
existing group/membership helpers, so this file holds no queries of its own.

Audiences:
- ``reviewers``      - the reviewer/admin group, plus the static ``LUMINA_REVIEW_NOTIFY_EMAILS``.
- ``submitter``      - the person who owns the object (``target.submitter`` or ``target.requester``).
- ``vendor_members`` - submit-role members of the object's owning vendor.
"""
from __future__ import annotations

from dataclasses import dataclass

REVIEWERS = "reviewers"
SUBMITTER = "submitter"
VENDOR_MEMBERS = "vendor_members"


@dataclass(frozen=True)
class Event:
    key: str
    audience: str
    subject: str
    description: str
    webhookable: bool = True


EVENTS: dict[str, Event] = {
    e.key: e
    for e in [
        # --- runs -----------------------------------------------------------
        Event("run.needs_details", SUBMITTER, "Your validation run needs details",
              "A validation run was uploaded and needs the submitter to add details and submit it."),
        Event("run.submitted", REVIEWERS, "A run was submitted for review",
              "A run entered the review queue."),
        Event("run.needs_changes", SUBMITTER, "Your run needs changes",
              "A reviewer sent a run back to its submitter."),
        Event("run.rejected", SUBMITTER, "Your run was not accepted",
              "A run was rejected."),
        Event("run.approved", SUBMITTER, "Your run was approved",
              "A run was approved and its evidence recorded."),
        # --- catalog submissions (hardware + software) ----------------------
        Event("submission.created", REVIEWERS, "A submission is awaiting review",
              "A catalog submission entered the review queue."),
        Event("submission.needs_changes", SUBMITTER, "Your submission needs changes",
              "A submission was sent back to its submitter."),
        Event("submission.rejected", SUBMITTER, "Your submission was not accepted",
              "A submission was rejected."),
        Event("submission.approved", SUBMITTER, "Your submission was approved",
              "A submission was approved and published."),
        # --- vendor claims --------------------------------------------------
        Event("vendor_claim.submitted", REVIEWERS, "A vendor claim is awaiting review",
              "Someone claimed a vendor and it needs review."),
        Event("vendor_claim.decided", SUBMITTER, "Update on your vendor claim",
              "A vendor claim was approved, rejected, or sent back."),
        # --- survey token requests ------------------------------------------
        Event("survey_token_request.submitted", REVIEWERS,
              "A survey token request is awaiting review",
              "Someone requested the ability to mint long-lived survey tokens."),
        Event("survey_token_request.decided", SUBMITTER,
              "Update on your survey token request",
              "A survey token request was approved, rejected, or sent back."),
        # --- edit proposals -------------------------------------------------
        Event("proposal.created", REVIEWERS, "A listing edit proposal is awaiting review",
              "A vendor proposed a correction to a catalog listing."),
    ]
}


def get(event_key: str) -> Event | None:
    return EVENTS.get(event_key)
