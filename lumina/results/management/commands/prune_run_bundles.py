"""Delete stored bundle/artifact files for old rejected runs.

The database rows (run, per-test results, metrics) are kept - only the bulky
files go. Approved runs keep their artifacts indefinitely; they are the
evidence behind published certification claims.
"""
from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from lumina.audit.services import log_action
from lumina.results.models import TestRun


class Command(BaseCommand):
    help = "Remove bundle and artifact files from rejected runs past retention."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=settings.LUMINA_REJECTED_BUNDLE_RETENTION_DAYS,
            help="Retention window in days (default from settings).",
        )

    def handle(self, *args, **options):
        cutoff = timezone.now() - timedelta(days=options["days"])
        stale = TestRun.objects.filter(
            status=TestRun.STATUS_REJECTED,
            reviewed_at__lt=cutoff,
        ).exclude(bundle="")
        pruned = 0
        for run in stale:
            for artifact in run.artifacts.all():
                artifact.file.delete(save=False)
            run.artifacts.all().delete()
            run.bundle.delete(save=False)
            run.bundle = ""
            run.save(update_fields=["bundle"])
            log_action("test_run.prune_bundle", target=run)
            pruned += 1
        self.stdout.write(self.style.SUCCESS(f"{pruned} run(s) pruned."))
