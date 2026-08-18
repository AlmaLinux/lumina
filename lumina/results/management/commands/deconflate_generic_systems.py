"""Split a System whose identity is a generic product line back out by motherboard.

Before generic product lines were recognized, every Supermicro reporting "Super Server" conflated
onto one System, pooling different machines' certifications. Migration 0012 detaches the not-yet-
published runs, but a System that was already **approved and attested** has to be split deliberately:
this re-homes each of its runs to the System its own board identifies (creating those as needed,
exactly as a fresh approval would), then deletes the generic System - CASCADE takes its old versions
and attestations with it, and each run's certification is re-recorded on the right board.

Idempotent and safe to re-run: a database with no generic-named systems changes nothing. Executes by
default; pass ``--dry-run`` to see what it would touch first.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from lumina.hardware.models import System
from lumina.results import services
from lumina.results.inventory_extract import is_generic_model


class Command(BaseCommand):
    help = "Split any System whose identity is a generic product line (e.g. 'Super Server') by board."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would be split without writing anything.",
        )

    def handle(self, *args, **options) -> None:
        dry_run = options["dry_run"]

        systems = [
            s for s in System.objects.select_related("vendor")
            if is_generic_model(s.vendor.name if s.vendor_id else "", s.name)
        ]
        if not systems:
            self.stdout.write("No generic-named systems to split.")
            return

        for system in systems:
            runs = list(system.test_runs.all())
            boards = sorted(
                {(r.board_vendor or "", r.board_model) for r in runs if (r.board_model or "").strip()}
            )
            board_desc = ", ".join(f"{v} {m}".strip() for v, m in boards) or "none identified"
            self.stdout.write(
                f"{system}: {len(runs)} run(s) across {len(boards)} board(s) [{board_desc}]"
            )
            if dry_run:
                continue

            with transaction.atomic():
                for run in runs:
                    run.listing_system = None
                    run.save(update_fields=["listing_system"])
                    # Re-home to the board's System and re-record its certification. The coupling
                    # self-gates on certifies()/verdict, so a non-certifying run is simply left
                    # unlinked to re-resolve on its next action.
                    services.ensure_component_ties(run)
                    if run.listing_system_id:
                        services.record_compatibility(run)
                        services._apply_attestation(run)
                name = str(system)
                # Delete the conflated evidence explicitly before the System itself. A version and
                # an attestation both point back at the System, so deleting it cascade-first hits a
                # reference cycle that Django breaks by NULLing a version's lone listing FK - which
                # MariaDB rejects (the "exactly one listing" check) and SQLite silently lets corrupt
                # the row. Clearing them first leaves the System with nothing cyclic to cascade; the
                # rest (category values, aliases, submissions) cascade cleanly on delete.
                from lumina.hardware.models import CommunityAttestation, ListingVersion
                ListingVersion.objects.filter(listing_system=system).delete()
                CommunityAttestation.objects.filter(listing_system=system).delete()
                system.delete()
                self.stdout.write(self.style.SUCCESS(f"  split and removed {name}"))

        if dry_run:
            self.stdout.write(self.style.WARNING(
                f"Dry run: {len(systems)} generic-named system(s) would be split."
            ))
