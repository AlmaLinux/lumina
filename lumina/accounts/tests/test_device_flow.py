"""Device-flow token strength, lifetime, and host binding."""
from __future__ import annotations

import pytest
from django.utils import timezone

pytestmark = pytest.mark.django_db


# --- token strength and host binding ------------------------------------------


def test_issued_tokens_are_64_chars():
    """Longer than needed against brute force, but these travel through
    scripts and logs, so the extra length is free."""
    from django.contrib.auth.models import User

    from lumina.accounts.models import ApiToken

    user = User.objects.create_user("len-check")
    _, raw = ApiToken.issue(user=user, name="t", scopes=["submit"])
    assert len(raw) == 64


def test_device_flow_binds_the_token_to_the_requesting_host(client):
    from django.contrib.auth.models import User

    from lumina.accounts.models import DeviceAuthRequest

    resp = client.post(
        "/api/v1/device/code",
        {"client_name": "alma-cert 0.1.0", "hostname": "sut-42.lab"},
        content_type="application/json",
    )
    assert resp.status_code == 200
    request = DeviceAuthRequest.objects.get()
    assert request.hostname == "sut-42.lab"

    approver = User.objects.create_user("approver")
    request.approve(by=approver)
    request.issue_token()
    request.refresh_from_db()
    assert request.token.hostname == "sut-42.lab"


def test_cli_tokens_last_twelve_hours(client):
    """A full validate-plus-benchmark pass must not outlive its own token.

    Goes through ``DeviceAuthRequest.issue_token`` rather than calling
    ``ApiToken.issue`` with the setting directly. The old version passed
    ``ttl_seconds`` itself, so it proved the setting held 12 hours and that
    ``ApiToken.issue`` honours an explicit TTL - but never that the device flow
    *supplies* it. Deleting ``ttl_seconds=`` from ``models.py:258`` silently gave
    CLI tokens the 30-day API default with the suite green.

    The setting assertion stays: it is the only lock on that constant anywhere.
    """
    from django.conf import settings
    from django.contrib.auth.models import User

    from lumina.accounts.models import DeviceAuthRequest

    assert settings.LUMINA_CLI_TOKEN_TTL_SECONDS == 12 * 60 * 60

    client.post(
        "/api/v1/device/code",
        {"client_name": "alma-cert 0.1.0", "hostname": "sut-42.lab"},
        content_type="application/json",
    )
    request = DeviceAuthRequest.objects.get()
    request.approve(by=User.objects.create_user("ttl-approver"))
    request.issue_token()
    request.refresh_from_db()

    remaining = (request.token.expires_at - timezone.now()).total_seconds()
    assert 11.9 * 3600 < remaining <= 12 * 3600
