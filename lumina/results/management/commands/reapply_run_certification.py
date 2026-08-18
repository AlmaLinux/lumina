"""Record what an approved run should already have: publish the listings it is tied to.

Approving a passing validation run publishes everything it ties - the machine, its board, its
CPU family, its GPUs, its NICs. Listings that came out of an approval still unpublished are
therefore a fault, and they exist: ``apply_run_certification`` used to apply certification
*before* the components were tied, so a listing whose first-ever run was that one attested to
nothing and never published. Its docstring records the ordering fix; this records the rows that
fix arrived too late for. A submitter sees them as "unpublished" on their dashboard next to runs
they watched a reviewer approve.

Idempotent, and safe on a database that never had the fault: every write underneath is a
``get_or_create`` or an already-true assignment, so a second run changes nothing.

**Creates no ties.** It records what the run's existing ties earned and resolves no part again -
a naming rule or an exclusion written since approval would resolve them differently, and a repair
that quietly refiles a machine's hardware under new names is worse than the fault.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from lumina.results.models import RunType, TestRun
from lumina.results.services import apply_run_certification, scoped_listings


class Command(BaseCommand):
    help = "Publish listings that an approved run should have published and did not."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would be published without writing anything.",
        )

    def handle(self, *args, **options) -> None:
        dry_run = options["dry_run"]
        runs = (
            TestRun.objects
            # Released, not merely approved: an embargoed run is withheld on purpose and
            # publish_due_runs is what applies its certification on the day.
            .filter(status=TestRun.STATUS_APPROVED, published_at__isnull=False,
                    run_type=RunType.validate.value)
            .select_related("listing_system", "submitter", "alma_release")
            .prefetch_related("listing_components")
            .order_by("pk")
        )
        repaired: list[str] = []
        # One line per listing, not per run: a component is commonly tied to several runs of the
        # same machine, and the prefetched rows still say unpublished after the first repair
        # published them, so without this the report counts the same part once per run.
        seen: set[tuple[str, int]] = set()
        for run in runs:
            unpublished = [
                listing for listing in scoped_listings(run) if not listing.published
            ]
            if not unpublished:
                continue
            for listing in unpublished:
                identity = (type(listing).__name__, listing.pk)
                if identity in seen:
                    continue
                seen.add(identity)
                repaired.append(f"  run {run.uuid}: {type(listing).__name__} {listing}")
            if not dry_run:
                with transaction.atomic():
                    apply_run_certification(run, tie=False)

        if not repaired:
            self.stdout.write("Nothing to repair: every approved run's listings are published.")
            return
        verb = "Would publish" if dry_run else "Published"
        self.stdout.write(f"{verb} {len(repaired)} listing(s):")
        for line in repaired:
            self.stdout.write(line)
