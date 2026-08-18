"""The reviewers' archive: what has already been decided, and by whom.

The queue only ever shows ``OPEN_STATUSES``. The moment a reviewer approves or rejects
something it vanishes from every page they can reach, so there was no way to answer
"what happened to that submission", "who approved this listing", or "what did I do last
Tuesday". The information was all recorded - each reviewable model keeps
``reviewed_by``/``reviewed_at``/``reviewer_notes``, and ``audit.AuditLogEntry`` has a row
per action - and none of it was readable outside the Django admin.

Which matters more than it sounds, because **reviewers are deliberately not staff**. The
review UI lives outside ``/admin/`` precisely so a reviewer needs no staff flag, and
``AuditLogEntryAdmin`` is therefore unreachable by exactly the people whose decisions it
records.

Two different things on one page, because they answer different questions:

- **Decisions** - one row per reviewed object, from the models themselves. This is the
  archive of the queue: the state as it stands now, with the note the reviewer left.
- **Activity** - ``AuditLogEntry`` rows, append-only, including actions that are not a
  decision about a reviewable object at all (a listing tweak, a taxonomy value promoted,
  a token issued, a bundle pruned). This is the audit trail proper, and unlike the
  decisions it keeps a history rather than only the latest state.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from django.urls import reverse


@dataclass(frozen=True)
class Source:
    """One reviewable model and how to describe a decision about it.

    Spelled out per model rather than derived from ``ReviewWorkflow`` subclasses. The
    mixin gives a uniform *state* but not a uniform *subject*: a hardware submission is
    about a listing, a software one about a product, a vendor claim about a vendor and a
    person. Guessing with ``str(obj)`` would print "Submission <uuid> (approved)" where a
    reviewer wants the machine's name.
    """

    label: str
    queryset: Callable[[], Any]
    subject: Callable[[Any], str]
    # Detail route, where one exists. Several reviewable things are only ever acted on
    # from the queue and have no page of their own; those rows are plain text rather than
    # dead links.
    url_name: str | None = None


def _sources() -> list[Source]:
    from lumina.hardware.models import ListingEditProposal, Submission
    from lumina.results.models import TestRun
    from lumina.software.models import SoftwareEditProposal, SoftwareSubmission
    from lumina.survey.models import SurveyTokenRequest
    from lumina.vendors.models import VendorClaim, VendorProposal

    decided = (Submission.STATUS_APPROVED, Submission.STATUS_REJECTED)
    return [
        Source(
            "Hardware submission",
            lambda: Submission.objects.filter(status__in=decided).select_related(
                "reviewed_by", "listing_system", "listing_component",
            ),
            lambda o: str(o.listing) if o.listing else "(listing deleted)",
            "review:detail",
        ),
        Source(
            "Validation run",
            lambda: TestRun.objects.filter(
                status__in=(TestRun.STATUS_APPROVED, TestRun.STATUS_REJECTED)
            ).select_related("reviewed_by", "listing_system"),
            # ``display_name`` is what every other page calls an unlinked machine, and it
            # falls back through the listing proposal and the board strings for a custom
            # build. A run that never got linked would otherwise print its uuid.
            lambda o: str(o.listing_system) if o.listing_system_id else o.display_name,
            "review:run_detail",
        ),
        Source(
            "Software submission",
            lambda: SoftwareSubmission.objects.filter(
                status__in=decided
            ).select_related("reviewed_by", "software"),
            lambda o: str(o.software),
        ),
        Source(
            "Listing edit",
            lambda: ListingEditProposal.objects.filter(
                status__in=decided
            ).select_related("reviewed_by", "listing_system", "listing_component"),
            lambda o: str(o.listing) if o.listing else "(listing deleted)",
            "review:listing_edit_detail",
        ),
        Source(
            "Software edit",
            lambda: SoftwareEditProposal.objects.filter(
                status__in=decided
            ).select_related("reviewed_by", "software"),
            lambda o: str(o.software),
        ),
        Source(
            "Vendor proposal",
            lambda: VendorProposal.objects.filter(status__in=decided).select_related(
                "reviewed_by", "target",
            ),
            lambda o: str(o.target) if o.target_id else o.name,
            "review:vendor_proposal_detail",
        ),
        Source(
            "Vendor claim",
            lambda: VendorClaim.objects.filter(status__in=decided).select_related(
                "reviewed_by", "vendor", "requester",
            ),
            lambda o: f"{o.vendor} - claimed by {o.requester}",
        ),
        Source(
            "Survey token request",
            lambda: SurveyTokenRequest.objects.filter(status__in=decided).select_related(
                "reviewed_by", "requester",
            ),
            lambda o: f"long-lived survey token for {o.requester}",
        ),
    ]


def decisions(*, limit: int = 200) -> list[dict]:
    """Everything already decided, newest decision first.

    Sorted in Python across seven models. A database-level union would need identical
    columns and these do not have them; at reviewer volumes the cost of sorting a few
    hundred rows is not worth the machinery, and ``limit`` is applied per source first so
    the work stays bounded however large the tables get.

    Rows with no ``reviewed_at`` sort last rather than crashing the comparison. That is a
    real case, not defensive padding: ``publish_due_runs`` releases an embargoed run on a
    timer with no reviewer attached, and older rows predate the column.
    """
    rows: list[dict] = []
    for source in _sources():
        for obj in source.queryset().order_by("-reviewed_at")[:limit]:
            rows.append({
                "kind": source.label,
                "subject": source.subject(obj),
                "outcome": obj.get_status_display(),
                "status": obj.status,
                "by": obj.reviewed_by,
                "at": obj.reviewed_at,
                "notes": (obj.reviewer_notes or "").strip(),
                "url": (
                    reverse(source.url_name, args=[obj.pk])
                    if source.url_name else None
                ),
            })
    rows.sort(key=lambda r: (r["at"] is not None, r["at"]), reverse=True)
    return rows[:limit]


def decision_kinds() -> list[str]:
    """The ``kind`` values the filter offers, in the order the sources are declared."""
    return [source.label for source in _sources()]
