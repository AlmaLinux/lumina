"""Re-derive the tier on every run-sourced attestation, then the columns it feeds.

Needed because ``effective_level`` now caps a vendor claim per listing. It used to resolve
the tier once per run and apply it to everything the run touched, so a Dell-attributed run
gave ``vendor`` to Dell's machine *and* to the Intel CPU family and NVIDIA GPU inside it.
Those rows are still in the database saying so, and nothing recomputes them on its own: an
attestation's level is frozen at approval and the listing's badge is derived from it.

Run once after deploying the cap. Idempotent, so running it twice is harmless and running it
on a database that never had the bug changes nothing.

Only attestations sourced from a **run** are touched. Submission-sourced ones were never
subject to this: ``Submission.approve`` caps them at ``MANUAL_CEILING`` and has no
per-listing vendor claim to get wrong.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from lumina.hardware.models import CommunityAttestation
from lumina.hardware.services import recompute_listing_levels
from lumina.results.services import effective_level


class Command(BaseCommand):
    help = "Re-derive run-sourced attestation tiers under the per-listing vendor cap."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would change without writing anything.",
        )

    def handle(self, *args, **options) -> None:
        dry_run = options["dry_run"]
        rows = (
            CommunityAttestation.objects
            .exclude(test_run=None)
            .select_related(
                "test_run", "test_run__on_behalf_of", "test_run__submitter",
                "version", "version__listing_system", "version__listing_component",
            )
        )

        changed = 0
        # Listings whose derived columns need redoing. Collected rather than recomputed per
        # row because one listing carries many attestations and the rollup reads all of them.
        touched: dict[tuple[str, int], object] = {}

        for attestation in rows:
            version = attestation.version
            listing = version.listing_system or version.listing_component
            if listing is None:
                continue
            correct = effective_level(attestation.test_run, listing)
            if correct == attestation.level:
                continue
            changed += 1
            # With the release named. One listing carries a row per major and their tiers
            # move independently, so "Intel Core 10th Generation: community -> vendor" twice
            # in a row is not a repeat and is not readable as two different facts without it.
            self.stdout.write(
                f"  {listing} on {version.release}: {attestation.level} -> {correct}"
            )
            if not dry_run:
                attestation.level = correct
                attestation.save(update_fields=["level"])
            touched[(type(listing).__name__, listing.pk)] = listing

        if dry_run:
            self.stdout.write(self.style.WARNING(
                f"Dry run: {changed} attestation(s) would change, "
                f"{len(touched)} listing(s) would be recomputed."
            ))
            return

        with transaction.atomic():
            for listing in touched.values():
                recompute_listing_levels(listing)

        self.stdout.write(self.style.SUCCESS(
            f"Re-derived {changed} attestation(s); "
            f"recomputed {len(touched)} listing(s)."
        ))
