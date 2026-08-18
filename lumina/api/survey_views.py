"""Standalone survey bundle ingest endpoint.

``POST /api/v1/survey/`` - an authenticated, submit-scoped upload of an
inventory-only survey bundle. Creates a ``SurveySubmission`` and never a
``TestRun``. Mirrors the auth, scope, and error handling of the results ingest
endpoint; there is deliberately no read side (the public never fetches an
individual submission - only aggregates).
"""
from __future__ import annotations

import hashlib
import hmac

from django.conf import settings
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from lumina.accounts.models import ApiToken, missing_scope
from lumina.results import ingest as results_ingest
from lumina.survey import ingest as survey_ingest
from lumina.survey.models import SurveySubmission


def _ip_hash(request: Request) -> str:
    """A salted hash of the client IP - kept for abuse triage, never the raw address."""
    ip = request.META.get("REMOTE_ADDR") or ""
    if not ip:
        return ""
    return hmac.new(
        settings.LUMINA_SURVEY_IDENTITY_PEPPER.encode("utf-8"),
        ip.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


class SurveyIngestView(APIView):
    """`/api/v1/survey/` - ingest an inventory-only survey bundle."""

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "survey-ingest"

    def post(self, request: Request):
        if not request.user.is_authenticated:
            return Response(
                {"code": "authentication_required", "detail": "Authentication required."},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        token = request.auth if isinstance(request.auth, ApiToken) else None
        if missing_scope(token, ApiToken.SCOPE_SUBMIT):
            return Response(
                {"code": "insufficient_scope", "detail": "Token lacks 'submit' scope."},
                status=status.HTTP_403_FORBIDDEN,
            )
        bundle = request.FILES.get("bundle")
        if bundle is None:
            return Response(
                {"code": "missing_bundle", "detail": "A 'bundle' file is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            submission = survey_ingest.ingest_survey_bundle(
                bundle_file=bundle,
                trust_tier=SurveySubmission.TIER_VERIFIED,
                submitter=request.user,
                token=token,
                source_ip_hash=_ip_hash(request),
            )
        except results_ingest.TooLarge as exc:  # subclass of BundleError - catch first
            return Response({"code": exc.code, "detail": exc.detail},
                            status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
        except results_ingest.BundleError as exc:
            return Response({"code": exc.code, "detail": exc.detail},
                            status=status.HTTP_400_BAD_REQUEST)

        return Response(
            {"uuid": str(submission.uuid), "trust_tier": submission.trust_tier},
            status=status.HTTP_201_CREATED,
        )
