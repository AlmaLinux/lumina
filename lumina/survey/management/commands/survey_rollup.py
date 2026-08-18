"""Recompute published survey aggregates from the append-only submissions.

Run by two systemd timers: the current month hourly, and a full rebuild of every period
nightly. Idempotent: each period is fully recomputed, so re-running changes nothing if
the submissions have not. The raw layer is never touched.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.utils import timezone

from lumina.survey.services import rebuild_survey_stats


def current_month() -> str:
    """This month as a period name, in the project's timezone.

    Django's ``__month`` lookup converts to the current timezone before comparing, so
    the period a submission falls in is decided in local time. Taking the month from
    ``date -u`` in a unit file instead would disagree with that for a few hours either
    side of the month boundary, and rebuild a period that is not the one filling up.
    """
    return timezone.localtime().strftime("%Y-%m")


class Command(BaseCommand):
    help = "Recompute survey statistics (SurveyStat) from submissions."

    def add_arguments(self, parser):
        which = parser.add_mutually_exclusive_group()
        which.add_argument(
            "--period", default=None,
            help='Limit to one period, e.g. "2026" or "2026-09". '
                 "Default: every period with data.",
        )
        which.add_argument(
            "--current", action="store_true",
            help="Limit to the current month. This is the only period that changes as "
                 "submissions arrive, so it is what a frequent rebuild needs to touch.",
        )

    def handle(self, *args, **options):
        period = current_month() if options["current"] else options["period"]
        periods = rebuild_survey_stats(period=period)
        self.stdout.write(self.style.SUCCESS(
            f"Rebuilt survey stats for {len(periods)} period(s): "
            f"{', '.join(periods) or 'none'}."
        ))
