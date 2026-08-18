"""Result ingestion and read endpoints.

Ingestion is synchronous: bundles are size-capped and parsing plus bulk
inserts complete in well under a request timeout, and the stack has no task
queue to hand work to. ``TestRun.status`` leaves room to add a ``received``
state and a worker later without changing this contract.
"""
from __future__ import annotations

from django.urls import reverse
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response

from lumina.accounts.models import ApiToken, missing_scope
from lumina.api.serializers import (
    BenchmarkResultSerializer,
    LeaderboardRowSerializer,
    TestRunDetailSerializer,
    TestRunSerializer,
)
from lumina.results import filters as result_filters
from lumina.results import ingest
from lumina.results.models import TestRun


class ResultViewSet(viewsets.ReadOnlyModelViewSet):
    """`/api/v1/results/` - ingest bundles, read public runs."""

    permission_classes = [AllowAny]
    lookup_field = "uuid"
    serializer_class = TestRunSerializer
    throttle_scope = "results-ingest"

    def get_throttles(self):
        # Only the write path is throttled; reads are public and cacheable.
        if self.action == "create":
            return super().get_throttles()
        return []

    def get_queryset(self):
        return result_filters.filter_runs(dict(self.request.GET.lists()))

    def get_object(self):
        # Submitters and reviewers can fetch their own non-public runs.
        return TestRun.objects.visible_to(self.request.user).get(
            uuid=self.kwargs["uuid"]
        )

    def retrieve(self, request: Request, *args, **kwargs):
        try:
            run = self.get_object()
        except TestRun.DoesNotExist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(TestRunDetailSerializer(run).data)

    def create(self, request: Request):
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

        # A device-flow token is bound to the machine the operator authorized.
        # Refusing results from any other host means a leaked token cannot be
        # used to post from elsewhere without also forging the hostname inside
        # the report. Not airtight - the client is open source - but it raises
        # the effort well past "paste the token somewhere else".
        if token is not None and token.hostname:
            reported = ingest.peek_hostname(bundle)
            if reported and reported != token.hostname:
                return Response(
                    {
                        "code": "hostname_mismatch",
                        "detail": (
                            f"This token was authorized for '{token.hostname}' "
                            f"but the results came from '{reported}'. Run "
                            "`alma-cert register` on that machine, or upload "
                            "the bundle through the web form."
                        ),
                    },
                    status=status.HTTP_403_FORBIDDEN,
                )

        try:
            run = ingest.ingest_bundle(
                submitter=request.user,
                bundle_file=bundle,
                source="api",
                pre_release=_optional_bool(request.data.get("pre_release")),
                publish_after=request.data.get("publish_after") or None,
                # ``--support-from-minor``. Ingest ignores it unless the report says the run was
                # on AlmaLinux Kitten, and drops junk rather than refusing the upload: losing a
                # whole run over a courtesy note to the reader would be the wrong trade.
                support_from_minor=request.data.get("support_from_minor") or None,
                submitter_notes=str(request.data.get("notes") or ""),
                # ``--anonymous``. Absent means "no instruction", which is not the same as
                # false: only then does the account-wide default get to decide, so this stays
                # a tri-state rather than defaulting to False here.
                publish_anonymously=_optional_bool(request.data.get("anonymous")),
            )
        except ingest.DuplicateRun as exc:
            if exc.identical:
                return Response(
                    {
                        "uuid": str(exc.run.uuid),
                        "status": exc.run.status,
                        "duplicate": True,
                        "web_url": _web_url(request, exc.run),
                    },
                    status=status.HTTP_200_OK,
                )
            return Response(
                {"code": exc.code, "detail": exc.detail},
                status=status.HTTP_409_CONFLICT,
            )
        except ingest.TooLarge as exc:
            return Response(
                {"code": exc.code, "detail": exc.detail},
                status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )
        except ingest.BundleError as exc:
            return Response(
                {"code": exc.code, "detail": exc.detail},
                status=status.HTTP_400_BAD_REQUEST,
            )

        from lumina.results.services import rule_excluded_summary

        return Response(
            {
                "uuid": str(run.uuid),
                "status": run.status,
                "run_type": run.run_type,
                # Echoed back so the collector can print what the server understood the claim to be.
                # A submitter whose ``--scope gpu`` run was stored as a whole-machine claim had no
                # way to find that out except by opening the review page, which is how the missing
                # field went unnoticed through a whole release of the suite.
                "claim_scope": run.claim_scope,
                # Devices a rule unticked by default (a BMC display adapter, an onboard iGPU), so the
                # submitter is told at upload rather than discovering it in review. A reviewer can
                # still include any of them. Empty when nothing matched.
                "excluded_components": rule_excluded_summary(run),
                "web_url": _web_url(request, run),
            },
            status=status.HTTP_201_CREATED,
        )


class BenchmarkViewSet(viewsets.ViewSet):
    """`/api/v1/benchmarks/` - catalog and leaderboards."""

    permission_classes = [AllowAny]
    lookup_value_regex = r"[\w.\-]+"

    def list(self, request: Request):
        return Response(result_filters.benchmark_catalog())

    @action(detail=True, methods=["get"])
    def leaderboard(self, request: Request, pk: str | None = None):
        params = dict(request.GET.lists())
        rows = result_filters.filter_leaderboard(benchmark_id=pk, params=params)[:200]
        version = params.get("version", [None])[0] or result_filters.latest_version_for(pk)
        return Response(
            {
                "benchmark_id": pk,
                "benchmark_version": version,
                "metric": (params.get("metric", [None])[0]
                           or result_filters.default_metric_for(pk, version)),
                "results": LeaderboardRowSerializer(rows, many=True).data,
            }
        )

    @action(detail=True, methods=["get"])
    def metrics(self, request: Request, pk: str | None = None):
        version = result_filters.latest_version_for(pk)
        return Response(result_filters.leaderboard_facets(pk, version))


def _optional_bool(value) -> bool | None:
    if value is None or value == "":
        return None
    return str(value).lower() in ("1", "true", "yes", "on")


def _web_url(request: Request, run: TestRun) -> str:
    return request.build_absolute_uri(
        reverse("results:run_detail", args=[run.uuid])
    )


__all__ = ["ResultViewSet", "BenchmarkViewSet", "BenchmarkResultSerializer"]
