"""Device-authorization endpoints for the certification suite CLI.

RFC 8628-shaped: the machine under test asks for a code, prints it, and polls
while the operator approves the request from a browser on any other device.
This keeps credentials off headless systems under test and ties every result
submission to a real Lumina account, which is what keeps anonymous garbage out
of the ingest endpoint.
"""
from __future__ import annotations

from django.conf import settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
    throttle_classes,
)
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

from lumina.accounts.auth import ApiTokenAuthentication
from lumina.accounts.models import DeviceAuthRequest, DeviceAuthStatus


class _DeviceCodeThrottle(ScopedRateThrottle):
    scope = "device-code"


class _DeviceTokenThrottle(ScopedRateThrottle):
    scope = "device-token"


def client_ip(request: Request) -> str | None:
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR") or None


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([_DeviceCodeThrottle])
def device_code(request: Request) -> Response:
    """Start a device-authorization request."""
    ip = client_ip(request)
    if ip and DeviceAuthRequest.objects.pending().filter(requester_ip=ip).count() >= (
        settings.LUMINA_DEVICE_MAX_PENDING_PER_IP
    ):
        return Response(
            {"error": "slow_down", "detail": "Too many pending requests from this address."},
            status=status.HTTP_429_TOO_MANY_REQUESTS,
        )

    client_name = str(request.data.get("client_name") or "alma-cert")[:80]
    device_request, raw_device_code = DeviceAuthRequest.start(
        client_name=client_name,
        hostname=str(request.data.get("hostname") or "")[:120],
        ip=ip,
    )
    return Response(
        {
            "device_code": raw_device_code,
            "user_code": device_request.user_code,
            "verification_uri": request.build_absolute_uri(
                reverse("accounts:activate")
            ),
            "expires_in": settings.LUMINA_DEVICE_CODE_TTL_SECONDS,
            "interval": settings.LUMINA_DEVICE_POLL_INTERVAL_SECONDS,
        }
    )


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([_DeviceTokenThrottle])
def device_token(request: Request) -> Response:
    """Poll for approval. Issues the token lazily so no raw token is stored."""
    raw = request.data.get("device_code")
    if not raw:
        return Response(
            {"error": "invalid_request", "detail": "device_code is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    device_request = DeviceAuthRequest.resolve(str(raw))
    if device_request is None:
        return Response(
            {"error": "invalid_grant", "detail": "Unknown device code."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    now = timezone.now()
    too_fast = (
        device_request.last_polled_at is not None
        and (now - device_request.last_polled_at).total_seconds()
        < settings.LUMINA_DEVICE_POLL_INTERVAL_SECONDS
    )
    DeviceAuthRequest.objects.filter(pk=device_request.pk).update(last_polled_at=now)
    if too_fast:
        return Response({"error": "slow_down"}, status=status.HTTP_400_BAD_REQUEST)

    if device_request.is_expired and device_request.status == DeviceAuthStatus.pending.value:
        return Response({"error": "expired_token"}, status=status.HTTP_400_BAD_REQUEST)
    if device_request.status == DeviceAuthStatus.denied.value:
        return Response({"error": "access_denied"}, status=status.HTTP_400_BAD_REQUEST)
    if device_request.status == DeviceAuthStatus.pending.value:
        return Response(
            {"error": "authorization_pending"}, status=status.HTTP_400_BAD_REQUEST
        )

    if device_request.token_id is not None:
        # The raw value only ever existed in the response to the first
        # successful poll; it cannot be replayed.
        return Response(
            {"error": "invalid_grant", "detail": "This code has already been used."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    raw_token = device_request.issue_token()
    return Response(
        {
            "token": raw_token,
            "expires_at": device_request.token.expires_at.isoformat(),
            "scopes": device_request.token.scopes,
            "user": device_request.approved_by.get_username(),
        }
    )


@api_view(["GET"])
# Token auth only, and it must be the *only* authenticator here for two reasons. It makes
# the endpoint answer exactly the question asked - "is this bearer token valid" - rather
# than also accepting a browser session, and DRF picks its rejection status from the first
# authenticator's ``authenticate_header``: with SessionAuthentication first (the project
# default) a bad token came back 403, which reads as "forbidden" rather than "your
# credential is no good".
@authentication_classes([ApiTokenAuthentication])
# Explicit, because the project default is IsAuthenticatedOrReadOnly and this is a GET -
# so without it an anonymous request sailed through and got ``{"valid": true}`` for the
# AnonymousUser. A CLI with no token at all would have been told its token was fine.
@permission_classes([IsAuthenticated])
def token_info(request: Request) -> Response:
    """Confirm a stored token is still usable, and say what it can do.

    The CLI's pre-run check used to trust the ``expires_at`` it had written to disk at
    registration. That is a claim about *time*, not about the token, and the two come apart
    for every reason a token stops working early: the operator revoked it, an admin deleted
    it, or - the case that produced this endpoint - the server's database was rebuilt while
    a perfectly unexpired token sat in the client's config. The run then completed and the
    upload failed at the very end, which is the most expensive moment to find out.

    Deliberately reachable by **any** scope, read-only included. Its whole job is to answer
    "is this credential real", so gating it behind the submit scope would make a read token
    unable to discover that it is a read token. The scope is in the response instead, which
    is what lets the CLI refuse a run that could never upload rather than discovering it
    after the fact.

    Authentication does the work: ``ApiTokenAuthentication`` resolves the bearer header and
    raises 401 for anything unknown, revoked, or expired, so reaching the body at all is the
    answer. Nothing here is secret - the caller already holds the token - and no part of
    the token itself is echoed back.
    """
    token = request.auth
    hostname = getattr(token, "hostname", "") or ""
    return Response({
        "valid": True,
        "username": request.user.get_username(),
        "scopes": list(getattr(token, "scopes", []) or []),
        "expires_at": (
            token.expires_at.isoformat()
            if getattr(token, "expires_at", None) else None
        ),
        "hostname": hostname,
    })
