"""How much is waiting in each reviewer queue, and the total across all of them.

Two callers need these numbers and must not disagree about them: the badge on each tab of
the review queue, and the badge on the sidebar link that says how much is waiting before
anybody opens the page. Defining the queues once here is what keeps a queue from being
counted on the page and missing from the total, which is the failure that would make the
sidebar quietly under-report.

Counted with ``COUNT(*)`` rather than by loading rows: the sidebar renders on every
signed-in page, and the queue page has the rows already.
"""
from __future__ import annotations

from django.core.cache import cache

# Cheap, but eleven COUNT queries on every page render is not free, and a nav badge does
# not need to be to-the-second. Short enough that acting on a queue updates the badge
# while the reviewer is still looking at it.
_CACHE_KEY = "review:queue-total"
_CACHE_SECONDS = 30


def _counters() -> dict:
    """One counter per reviewer queue. Imported lazily: this module is loaded by a
    template tag, which Django imports while the app registry is still starting up."""
    from lumina.hardware.models import ListingEditProposal, Submission
    from lumina.results.models import RunType, TestRun
    from lumina.software.models import (
        SoftwareCompatibility,
        SoftwareEditProposal,
        SoftwareSubmission,
    )
    from lumina.survey.models import SurveySubmission, SurveyTokenRequest
    from lumina.vendors.models import VendorClaim, VendorProposal

    return {
        # Keys match the tab they sit under, so a reader can line the two up.
        "submissions": lambda: Submission.objects.filter(
            status__in=Submission.OPEN_STATUSES).count(),
        "vendors": lambda: (
            VendorProposal.objects.filter(
                status__in=VendorProposal.OPEN_STATUSES).count()
            + VendorClaim.objects.filter(
                status__in=VendorClaim.OPEN_STATUSES).count()
        ),
        "listing_edits": lambda: ListingEditProposal.objects.filter(
            status__in=ListingEditProposal.OPEN_STATUSES).count(),
        "software": lambda: (
            SoftwareSubmission.objects.filter(
                status__in=SoftwareSubmission.OPEN_STATUSES).count()
            + SoftwareEditProposal.objects.filter(
                status__in=SoftwareEditProposal.OPEN_STATUSES).count()
            + SoftwareCompatibility.objects.pending().count()
        ),
        # Validation and benchmark runs are two tabs and two decisions, so two counters.
        "validation_runs": lambda: TestRun.objects.open_for_review().exclude(
            run_type=RunType.benchmark.value).count(),
        "benchmark_runs": lambda: TestRun.objects.open_for_review().filter(
            run_type=RunType.benchmark.value).count(),
        "quarantined_runs": lambda: TestRun.objects.quarantined().count(),
        "survey_tokens": lambda: SurveyTokenRequest.objects.filter(
            status__in=SurveyTokenRequest.OPEN_STATUSES).count(),
        "survey": lambda: SurveySubmission.objects.pending_review().count(),
    }


def queue_counts() -> dict[str, int]:
    """Every queue's depth, by key. Uncached: callers that want one number use ``total``."""
    return {key: counter() for key, counter in _counters().items()}


def total(*, use_cache: bool = True) -> int:
    """Everything waiting on a reviewer, across every queue.

    This is the number on the sidebar link, so it answers "is there anything to do at
    all" from any page. Quarantined runs are included: they are a decision somebody has
    to make, even though they are held for a different reason from the rest.
    """
    if not use_cache:
        return sum(queue_counts().values())
    cached = cache.get(_CACHE_KEY)
    if cached is None:
        cached = sum(queue_counts().values())
        cache.set(_CACHE_KEY, cached, _CACHE_SECONDS)
    return cached
