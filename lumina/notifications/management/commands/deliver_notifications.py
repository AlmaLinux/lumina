"""Drain the notification outbox: fan queued events out to recipients and deliver the due ones.

Run periodically - the ansible role installs a systemd timer firing every minute. Idempotent and
safe to overlap: events are fanned out once (``processed_at``) under a row lock, and each delivery
is claimed with a compare-and-set before sending, so a second run in flight neither double-fans nor
double-sends. All the slow SMTP/HTTP work lives here, off the request path.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from lumina.notifications import services


class Command(BaseCommand):
    help = "Deliver pending notification events (email + webhooks) from the outbox."

    def add_arguments(self, parser):
        parser.add_argument(
            "--max-events", type=int, default=500,
            help="Most events to fan out this run (default 500).",
        )
        parser.add_argument(
            "--max-deliveries", type=int, default=500,
            help="Most deliveries to attempt this run (default 500).",
        )

    def handle(self, *args, **options):
        result = services.deliver_pending(
            max_events=options["max_events"], max_deliveries=options["max_deliveries"],
        )
        self.stdout.write(
            "Fanned out {fanned_out} event(s), attempted {attempted} deliver(y/ies).".format(**result)
        )
