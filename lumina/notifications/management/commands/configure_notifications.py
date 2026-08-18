"""Apply the notification routing the deployment configures, from the environment into the database.

Run on every deploy by the ansible role, and again after a database reset, where nothing else would
put the chat endpoint back. Idempotent: a run that finds everything already right changes nothing
and says so, so rotating the webhook URL in vault (or in the dev host's GitHub secret) and
redeploying is the whole procedure.

Nothing here prints the URL. A Mattermost incoming-webhook URL *is* the credential - anybody
holding it can post into the channel - and this runs where its output is a CI log.
"""
from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from lumina.notifications import provisioning


class Command(BaseCommand):
    help = "Configure the notification endpoints and routing this deployment owns."

    def handle(self, *args, **options):
        url = getattr(settings, "LUMINA_MATTERMOST_WEBHOOK_URL", "")
        channel = getattr(settings, "LUMINA_MATTERMOST_CHANNEL", "")
        if not url.strip():
            self.stdout.write(
                "LUMINA_MATTERMOST_WEBHOOK_URL is not set: no chat endpoint is configured by this "
                "deployment. Anything set up in the admin is untouched."
            )
            return
        try:
            outcome = provisioning.configure_mattermost(url=url, channel=channel)
        except ValidationError as exc:
            # A failed deploy, deliberately. Stored instead, a bad URL is only visible later as
            # every delivery exhausting its attempts against an address nobody reads.
            raise CommandError(
                f"LUMINA_MATTERMOST_WEBHOOK_URL is not a usable URL: {'; '.join(exc.messages)}"
            ) from exc

        endpoint = outcome.endpoint
        if endpoint is None:  # unreachable: a non-blank URL always leaves one
            return
        where = f"#{channel.strip().lstrip('#')}" if channel.strip() else "its own channel"
        self.stdout.write(
            f"{'Created' if outcome.created else 'Found'} the {endpoint.name} endpoint"
            f"{', with a new URL' if outcome.url_changed else ''}, posting to {where}."
        )
        self.stdout.write(
            f"Routed {len(outcome.routed)} new event(s); moved {len(outcome.rechanneled)}; "
            f"{len(provisioning.deployment_events())} reviewer event(s) post there in total."
        )
        if not endpoint.enabled:
            self.stdout.write(self.style.WARNING(
                f"The {endpoint.name} endpoint is disabled in the admin, so nothing will be posted "
                "to it. Deploying does not re-enable it: switching it off there is a decision."
            ))
