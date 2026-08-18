"""Device-authorization flow: RFC 8628 semantics end to end."""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone

from lumina.accounts.models import ApiToken, DeviceAuthRequest

pytestmark = pytest.mark.django_db

CODE_URL = "/api/v1/device/code"
TOKEN_URL = "/api/v1/device/token"


@pytest.fixture(autouse=True)
def _no_throttle(settings):
    settings.REST_FRAMEWORK = {
        **settings.REST_FRAMEWORK,
        "DEFAULT_THROTTLE_RATES": {
            "results-ingest": None, "device-code": None, "device-token": None,
        },
    }


@pytest.fixture
def approver():
    return User.objects.create_user("operator", password="pw")


def _start(client):
    resp = client.post(CODE_URL, {"client_name": "alma-cert 0.1.0"},
                       content_type="application/json")
    assert resp.status_code == 200
    return resp.json()


def _fresh_poll(client, device_code):
    """Poll with the slow-down window cleared, as a well-behaved client that
    waited `interval` seconds would."""
    DeviceAuthRequest.objects.update(last_polled_at=None)
    return client.post(TOKEN_URL, {"device_code": device_code},
                       content_type="application/json")


def test_code_issue_shape(client):
    body = _start(client)
    assert set(body) >= {"device_code", "user_code", "verification_uri",
                         "expires_in", "interval"}
    assert "/my/activate/" in body["verification_uri"]
    # user code is short, unambiguous, and grouped for reading aloud
    assert len(body["user_code"]) == 9 and body["user_code"][4] == "-"
    # only the hash is stored
    stored = DeviceAuthRequest.objects.get()
    assert body["device_code"] not in (stored.device_code_hash, stored.user_code)


def test_poll_pending_then_approve_then_token(client, approver):
    body = _start(client)

    resp = _fresh_poll(client, body["device_code"])
    assert resp.status_code == 400
    assert resp.json()["error"] == "authorization_pending"

    request_row = DeviceAuthRequest.objects.get()
    request_row.approve(by=approver)

    resp = _fresh_poll(client, body["device_code"])
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["scopes"] == [ApiToken.SCOPE_SUBMIT]
    assert payload["user"] == "operator"

    token = ApiToken.resolve(payload["token"])
    assert token is not None
    assert token.user == approver
    assert token.has_scope(ApiToken.SCOPE_SUBMIT)


def test_token_single_use(client, approver):
    body = _start(client)
    DeviceAuthRequest.objects.get().approve(by=approver)
    assert _fresh_poll(client, body["device_code"]).status_code == 200
    resp = _fresh_poll(client, body["device_code"])
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"


def test_denied_request(client, approver):
    body = _start(client)
    DeviceAuthRequest.objects.get().deny(by=approver)
    resp = _fresh_poll(client, body["device_code"])
    assert resp.json()["error"] == "access_denied"


def test_expired_request(client):
    body = _start(client)
    DeviceAuthRequest.objects.update(expires_at=timezone.now())
    resp = _fresh_poll(client, body["device_code"])
    assert resp.json()["error"] == "expired_token"


def test_unknown_device_code(client):
    resp = client.post(TOKEN_URL, {"device_code": "nonsense"},
                       content_type="application/json")
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"


def test_fast_polling_gets_slow_down(client):
    body = _start(client)
    first = client.post(TOKEN_URL, {"device_code": body["device_code"]},
                        content_type="application/json")
    assert first.json()["error"] == "authorization_pending"
    # immediate re-poll violates the advertised interval
    second = client.post(TOKEN_URL, {"device_code": body["device_code"]},
                         content_type="application/json")
    assert second.json()["error"] == "slow_down"


def test_pending_per_ip_cap(client, settings):
    settings.LUMINA_DEVICE_MAX_PENDING_PER_IP = 2
    _start(client)
    _start(client)
    resp = client.post(CODE_URL, {"client_name": "x"}, content_type="application/json")
    assert resp.status_code == 429


# --- browser activation -------------------------------------------------------


def test_activate_page_flow(client, approver):
    body = _start(client)
    client.force_login(approver)

    # step 1: enter the code
    resp = client.post(reverse("accounts:activate"), {"user_code": body["user_code"]})
    assert resp.status_code == 200
    assert body["user_code"] in resp.text
    assert "alma-cert 0.1.0" in resp.text

    # step 2: confirm
    request_row = DeviceAuthRequest.objects.get()
    resp = client.post(
        reverse("accounts:activate_confirm", args=[request_row.pk]),
        {"decision": "approve"},
    )
    assert resp.status_code == 302
    request_row.refresh_from_db()
    assert request_row.status == "approved"
    assert request_row.approved_by == approver


def test_activate_requires_login(client):
    resp = client.get(reverse("accounts:activate"))
    assert resp.status_code == 302  # to login


def test_activate_lockout_after_failed_attempts(client, approver):
    client.force_login(approver)
    url = reverse("accounts:activate")
    for _ in range(5):
        client.post(url, {"user_code": "XXXX-XXXX"})
    resp = client.post(url, {"user_code": "XXXX-XXXX"})
    assert b"Too many attempts" in resp.content


def test_normalizes_lowercase_and_missing_dash(client, approver):
    body = _start(client)
    client.force_login(approver)
    raw = body["user_code"].replace("-", "").lower()
    resp = client.post(reverse("accounts:activate"), {"user_code": raw})
    assert "alma-cert 0.1.0" in resp.text
