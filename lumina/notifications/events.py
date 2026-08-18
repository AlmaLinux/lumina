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

# How a chat message about the event should read at a glance. Rendered as the colour of a
# Mattermost attachment's left bar, which is the one part of a rich message a reader takes
# in before any words: "something of mine was rejected" should not look like "something is
# waiting for you". Platform-neutral names, because the mapping to a colour belongs to
# whichever chat platform is rendering it.
TONE_INFO = "info"          # something happened, nothing is wrong
TONE_ATTENTION = "attention"  # somebody has to act
TONE_GOOD = "good"          # it went well
TONE_BAD = "bad"            # it did not


@dataclass(frozen=True)
class Event:
    key: str
    audience: str
    subject: str
    description: str
    webhookable: bool = True
    tone: str = TONE_INFO
    # For a reviewer event: which queue tab the link should open. The queue is nine panes
    # deep, and a mail saying "a survey submission is waiting" that lands on hardware
    # submissions makes the reader hunt for it. Validated against the panes that exist
    # when the URL is built. Blank means the queue's default tab.
    tab: str = ""


EVENTS: dict[str, Event] = {
    e.key: e
    for e in [
        # --- runs -----------------------------------------------------------
        Event("run.needs_details", SUBMITTER, "Your validation run needs details",
              "A validation run was uploaded and needs the submitter to add details and submit it.", tone=TONE_ATTENTION),
        # No tab: a run is either a validation or a benchmark and they are separate panes,
        # so this one cannot say which without reading the target.
        Event("run.submitted", REVIEWERS, "A run was submitted for review",
              "A run entered the review queue.", tone=TONE_ATTENTION),
        Event("run.needs_changes", SUBMITTER, "Your run needs changes",
              "A reviewer sent a run back to its submitter.", tone=TONE_ATTENTION),
        Event("run.rejected", SUBMITTER, "Your run was not accepted",
              "A run was rejected.", tone=TONE_BAD),
        Event("run.approved", SUBMITTER, "Your run was approved",
              "A run was approved and its evidence recorded.", tone=TONE_GOOD),
        # --- catalog submissions (hardware + software) ----------------------
        Event("submission.created", REVIEWERS, "A submission is awaiting review",
              "A catalog submission entered the review queue.", tab="submissions", tone=TONE_ATTENTION),
        Event("submission.needs_changes", SUBMITTER, "Your submission needs changes",
              "A submission was sent back to its submitter.", tone=TONE_ATTENTION),
        Event("submission.rejected", SUBMITTER, "Your submission was not accepted",
              "A submission was rejected.", tone=TONE_BAD),
        Event("submission.approved", SUBMITTER, "Your submission was approved",
              "A submission was approved and published.", tone=TONE_GOOD),
        # --- vendor claims --------------------------------------------------
        Event("vendor_claim.submitted", REVIEWERS, "A vendor claim is awaiting review",
              "Someone claimed a vendor and it needs review.", tab="vendors", tone=TONE_ATTENTION),
        Event("vendor_claim.decided", SUBMITTER, "Update on your vendor claim",
              "A vendor claim was approved, rejected, or sent back.", tone=TONE_INFO),
        # --- survey token requests ------------------------------------------
        Event("survey_token_request.submitted", REVIEWERS,
              "A survey token request is awaiting review",
              "Someone requested the ability to mint long-lived survey tokens.",
              tab="survey-tokens", tone=TONE_ATTENTION),
        # --- survey submissions ----------------------------------------------
        # Only a standalone survey run raises this. A cert-run fork is a byproduct of a
        # validate or benchmark run that is reviewable in its own queue, so it is never
        # queued here and must never mail anybody: it would double the traffic for every
        # certification run and say nothing new.
        Event("survey.submitted", REVIEWERS,
              "A hardware survey submission is awaiting review",
              "A hardware survey submission entered the moderation queue. It already "
              "counts toward the published statistics; review is oversight, not a gate.",
              tab="survey", tone=TONE_ATTENTION),
        Event("survey_token_request.decided", SUBMITTER,
              "Update on your survey token request",
              "A survey token request was approved, rejected, or sent back.", tone=TONE_INFO),
        # --- edit proposals -------------------------------------------------
        Event("proposal.created", REVIEWERS, "A listing edit proposal is awaiting review",
              "A vendor proposed a correction to a catalog listing.",
              tone=TONE_ATTENTION, tab="edits"),
    ]
}


def get(event_key: str) -> Event | None:
    return EVENTS.get(event_key)
