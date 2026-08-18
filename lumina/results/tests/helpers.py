"""Shared test helpers for the certification-run workflow."""
from __future__ import annotations

from lumina.results import services
from lumina.results.models import TestRun


def release(run: TestRun, submitter=None) -> TestRun:
    """Move a draft validation run into the review queue.

    Ingest deliberately leaves validation runs as drafts so the submitter can
    supply the listing detail the suite cannot derive. Tests about *review*
    use this to stand in for that step; tests about the completion flow
    itself drive the real form instead.
    """
    if run.status != TestRun.STATUS_DRAFT:
        return run
    if services.missing_submission_details(run):
        run.listing_proposal = {
            "vendor_name": run.system_vendor, "name": run.system_product,
        }
        run.save(update_fields=["listing_proposal"])
    services.submit_for_review(run, by=submitter or run.submitter)
    run.refresh_from_db()
    return run
