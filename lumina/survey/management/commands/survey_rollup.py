"""Recompute published survey aggregates from the append-only submissions.

Run by a systemd timer (daily). Idempotent: each period is fully recomputed, so
re-running changes nothing if the submissions have not. The raw layer is never
touched.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from lumina.survey.services import rebuild_survey_stats


class Command(BaseCommand):
    help = "Recompute survey statistics (SurveyStat) from submissions."

    def add_arguments(self, parser):
        parser.add_argument(
            "--period", default=None,
            help='Limit to one period, e.g. "2026". Default: every period with data.',
        )

    def handle(self, *args, **options):
        periods = rebuild_survey_stats(period=options["period"])
        self.stdout.write(self.style.SUCCESS(
            f"Rebuilt survey stats for {len(periods)} period(s): {', '.join(periods) or 'none'}."
        ))
