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
        published: list[str] = []
        attempted: list[str] = []
        still_stuck: list[str] = []
        # One line per listing, not per run: a component is commonly tied to several runs of the
        # same machine, so without this the report counts the same part once per run.
        seen: set[tuple[str, int]] = set()
        for run in runs:
            unpublished = [
                listing for listing in scoped_listings(run) if not listing.published
            ]
            if not unpublished:
                continue
            fresh = []
            for listing in unpublished:
                identity = (type(listing).__name__, listing.pk)
                if identity in seen:
                    continue
                seen.add(identity)
                fresh.append(listing)
            attempted.extend(f"  run {run.uuid}: {_name(listing)}" for listing in fresh)
            if dry_run:
                continue
            with transaction.atomic():
                apply_run_certification(run, tie=False)
            # Re-read, rather than assume. This reported what it *intended* to publish and
            # never looked at the result, so a repair that did nothing - which is what happened
            # to every Kitten run, whose release row was never created - printed six names and
            # a success. A command that cannot tell you whether it worked is worse than one
            # that refuses to run.
            for listing in fresh:
                line = f"  run {run.uuid}: {_name(listing)}"
                (published if _is_published(listing) else still_stuck).append(line)

        if not attempted:
            self.stdout.write("Nothing to repair: every approved run's listings are published.")
            return

        if dry_run:
            self.stdout.write(f"Would try {len(attempted)} listing(s):")
            for line in attempted:
                self.stdout.write(line)
            return

        if published:
            self.stdout.write(f"Published {len(published)} listing(s):")
            for line in published:
                self.stdout.write(line)
        if still_stuck:
            self.stdout.write(self.style.ERROR(
                f"{len(still_stuck)} listing(s) did not publish:"))
            for line in still_stuck:
                self.stdout.write(line)
            self.stdout.write(
                "The run certified nothing for these. Check `certifies(run)` and that the run "
                "names an AlmaLinux release; the audit log records a "
                "test_run.certification_incomplete entry for each such approval."
            )


def _name(listing) -> str:
    return f"{type(listing).__name__} {listing}"


def _is_published(listing) -> bool:
    """Ask the database, rather than the object we were holding.

    ``refresh_from_db`` would do here too, and only by luck: ``scoped_listings`` reaches the
    system through a cached FK and the components through a prefetch, so the rows certification
    mutates are the very objects this loop holds. Depending on that is the same accidental
    coupling that produced the fault this command exists to repair, and it would stop being true
    the day anything re-queried.
    """
    return type(listing).objects.filter(pk=listing.pk, published=True).exists()
