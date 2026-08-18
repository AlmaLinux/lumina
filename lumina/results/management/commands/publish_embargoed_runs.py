"""Release approved runs whose embargo date has arrived.

Run periodically (the ansible role installs a systemd timer firing every
15 minutes). Idempotent: already-published runs are never touched.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from lumina.results.services import publish_due_runs


class Command(BaseCommand):
    help = "Publish approved runs whose requested publish date has passed."

    def handle(self, *args, **options):
        published = publish_due_runs()
        for run in published:
            self.stdout.write(f"published {run.uuid} ({run.display_name})")
        self.stdout.write(
            self.style.SUCCESS(f"{len(published)} run(s) published.")
        )
