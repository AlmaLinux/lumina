"""Writing survey submissions - the one append-only entry point.

Every survey record, whether a standalone run or a fork from a certification
ingest, is created here (as audit writes go through ``log_action``). Extraction,
catalog-free normalization, and identity hashing happen here, so callers pass
only the raw report sections and provenance.
"""
from __future__ import annotations

import logging
from collections import defaultdict

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from lumina.audit.services import log_action
from lumina.notifications.services import emit
from lumina.results import exclusions
from lumina.survey import devices, extract, identity, normalize
from lumina.survey.models import (
    SurveySegment,
    SurveyStat,
    SurveySubmission,
    SurveyTokenGrant,
    SurveyTokenRequest,
)

logger = logging.getLogger(__name__)


def record_submission(*, inventory, environment, origin, trust_tier,
                      submitter=None, token=None, source_ip_hash="") -> SurveySubmission:
    """Create one append-only ``SurveySubmission`` from a raw report.

    ``inventory``/``environment`` are the verbatim report sections. Facets and
    the derived identity hash are computed here; the raw payload is stored as-is.
    """
    columns = extract.survey_extract(inventory or {}, environment or {})
    identity_hash, identity_source = identity.hash_identity(
        pepper=settings.LUMINA_SURVEY_IDENTITY_PEPPER,
        smbios_uuid=columns["system_uuid"],
        board_serial=columns["board_serial"],
        machine_id=columns["machine_id"],
    )
    return SurveySubmission.objects.create(
        origin=origin,
        trust_tier=trust_tier,
        submitter=submitter,
        token=token,
        source_ip_hash=source_ip_hash,
        inventory=inventory or {},
        identity_hash=identity_hash,
        identity_source=identity_source,
        **columns,
    )


def fork_survey_record(run) -> SurveySubmission | None:
    """Fork a survey record from an ingested certification run.

    The census stream is independent of certification: this is isolated in a
    savepoint and never allowed to raise, so a survey-side failure cannot roll
    back the ingest. If the whole ingest rolls back for other reasons, this
    savepoint is discarded with it. Returns the record, or None if the survey is
    disabled or the fork failed.
    """
    if not getattr(settings, "LUMINA_SURVEY_ENABLED", True):
        return None
    try:
        with transaction.atomic():
            return record_submission(
                inventory=run.inventory,
                environment=run.environment,
                origin=SurveySubmission.ORIGIN_CERT_RUN,
                # A cert run was authenticated to submit for review, so its survey
                # record is verified at no extra cost.
                trust_tier=SurveySubmission.TIER_VERIFIED,
                submitter=run.submitter,
            )
    except Exception:  # a survey bug must never break certification ingest
        logger.exception("survey fork failed for run %s", getattr(run, "uuid", "?"))
        return None


# --- rollup: append-only submissions -> published aggregates ------------------
#
# Everything below is the derived layer. It reads submissions and rewrites the
# SurveyStat rollup; it never mutates a submission. Dedup, tier weighting, and
# bucketing all live here, not in storage.

_MEM_BUCKETS_GB = (8, 16, 32, 64, 128, 256, 512, 1024, 2048)


def _mem_bucket(memory_bytes) -> str:
    if not memory_bytes:
        return ""
    gb = memory_bytes / (1024 ** 3)
    lo = 0
    for hi in _MEM_BUCKETS_GB:
        if gb <= hi:
            return f"{lo}-{hi} GB"
        lo = hi
    return f"{_MEM_BUCKETS_GB[-1]}+ GB"


def _dimensions(sub: SurveySubmission, rules: list | None = None):
    """(dimension, bucket) pairs for one machine. Blank buckets are dropped by the tally.

    The GPU is re-derived from the stored payload rather than read off the extracted
    column, and that is the point rather than an inefficiency. The column holds what the
    machine reported, which on a server is usually the BMC's display adapter; the census
    should count the GPU a person would name, which is the same judgement the review
    screen already makes. Deriving it here, in the report layer, means the rule applies
    to every submission ever made the next time the rollup runs, including the ones
    already stored, and an admin adding an exclusion row tomorrow corrects yesterday's
    numbers. That is what "raw in, aggregate out" was for.
    """
    countable = devices.countable_gpu(sub.inventory, rules)
    if countable is None:
        # The payload lists no GPUs at all, so there is nothing to judge: keep what was
        # extracted at ingest rather than recording that the machine has no GPU.
        gpu_vendor, gpu_model = sub.gpu_vendor, sub.gpu_model
    else:
        gpu_vendor, gpu_model = countable

    yield "cpu_model", sub.cpu_model
    yield "cpu_vendor", sub.cpu_vendor
    yield "cpu_sockets", str(sub.cpu_sockets) if sub.cpu_sockets else ""
    yield "gpu_vendor", gpu_vendor
    yield "gpu_model", normalize.gpu_model(gpu_model or "")
    yield "board_vendor", sub.board_vendor
    yield "arch", sub.arch
    yield "x86_64_level", sub.x86_64_level
    yield "memory", _mem_bucket(sub.memory_bytes)
    yield "kernel", sub.kernel
    yield "os_version", (f"{sub.os_major}.{sub.os_minor}"
                         if sub.os_major and sub.os_minor is not None else "")


def _dedup(submissions) -> list:
    """Most-recent submission per machine. ``identity_hash`` keys a machine; a blank
    hash (no usable identity) cannot be deduped, so each such row is its own machine.

    Expects ``submissions`` ordered most-recent first, so the first seen per key wins.
    """
    seen: dict[str, SurveySubmission] = {}
    for sub in submissions:
        key = sub.identity_hash or f"anon:{sub.uuid}"
        if key not in seen:
            seen[key] = sub
    return list(seen.values())


def _period_filter(period: str) -> dict:
    """The ``received_at`` lookup one period name selects.

    Two granularities share the table, distinguished by the shape of the name:
    ``"2026"`` is a year and ``"2026-09"`` is a month. Both are real periods rather
    than one being derived from the other, because dedup happens inside a period - a
    machine that reports every month is one machine in each of those months and one
    machine in the year, and summing the months would count it twelve times.
    """
    year, _, month = period.partition("-")
    if month:
        return {"received_at__year": int(year), "received_at__month": int(month)}
    return {"received_at__year": int(year)}


def _period_rows(period: str, segment=None) -> list[SurveyStat]:
    """Rollup rows for one period, optionally narrowed to one named cohort.

    The narrowing happens *before* dedup, which is the whole point of a segment: a cohort
    is counted in its own right, so its shares add to 100% of the cohort rather than
    being a slice of the whole-fleet percentages.
    """
    rows: list[SurveyStat] = []
    name = segment.slug if segment is not None else ""
    for tier_scope in (SurveyStat.TIER_VERIFIED, SurveyStat.TIER_ALL):
        qs = (SurveySubmission.objects.countable()
              .filter(**_period_filter(period))
              .order_by("-received_at", "-id"))
        if segment is not None:
            qs = segment.narrow(qs)
        if tier_scope == SurveyStat.TIER_VERIFIED:
            qs = qs.filter(trust_tier=SurveySubmission.TIER_VERIFIED)
        tally: dict[tuple[str, str], int] = defaultdict(int)
        machines = _dedup(qs)
        # Fetched once for the whole period rather than per machine: the exclusion rules
        # are a handful of rows and this is inside a loop over every surveyed machine.
        rules = exclusions.active_rules()
        # The machine count for the period, which no facet can stand in for: each of those
        # totals only the machines that reported it. See SurveyStat.MACHINES_DIMENSION.
        #
        # Omitted at zero, like an empty facet bucket. A count-of-nothing row would make
        # the period look present for a cohort that has no machines in it, and the period
        # picker reads exactly that, so it would offer a period whose page is empty.
        if machines:
            tally[(SurveyStat.MACHINES_DIMENSION, SurveyStat.MACHINES_BUCKET)] = len(machines)
        for machine in machines:
            for dimension, bucket in _dimensions(machine, rules):
                if bucket:  # no empty buckets; a single machine still counts (no suppression)
                    tally[(dimension, bucket)] += 1
        rows.extend(
            SurveyStat(segment=name, period=period, dimension=dimension, bucket=bucket,
                       count=count, dedup_key="identity", tier_scope=tier_scope)
            for (dimension, bucket), count in tally.items()
        )
    return rows


def _all_periods() -> list[str]:
    """Every year and every month that has submissions in it.

    Months are what the trend charts read: a year is a single point and says nothing
    about which way anything is moving. Years are kept as the headline scope, since a
    machine reporting monthly should count once in the annual picture.
    """
    countable = SurveySubmission.objects.countable()
    years = [str(d.year) for d in countable.dates("received_at", "year")]
    months = [f"{d.year}-{d.month:02d}"
              for d in countable.dates("received_at", "month")]
    return years + months


def rebuild_survey_stats(*, period=None) -> list[str]:
    """Recompute ``SurveyStat`` for one period or every period with data.

    Idempotent by construction: each period's rows are deleted and rebuilt in one
    transaction, so re-running over unchanged submissions produces the same table.
    Returns the periods rebuilt.

    Every segment is rebuilt alongside the whole fleet, including disabled ones: a
    segment that is switched back on should not have a hole in its history, and the cost
    of keeping it current is the same pass either way. This is one pass per segment per
    period, which is the reason segments are admin-curated rather than visitor-defined.
    """
    periods = [period] if period else _all_periods()
    # None is the whole fleet, and goes first so a partial failure leaves the page's
    # default view current rather than only its cohorts.
    cohorts = [None, *SurveySegment.objects.all()]
    for one in periods:
        with transaction.atomic():
            SurveyStat.objects.filter(period=one).delete()
            for cohort in cohorts:
                SurveyStat.objects.bulk_create(_period_rows(one, cohort))
    return periods


# --- token capability ---------------------------------------------------------
#
# A long-lived, submit-scoped token is what fleets and background surveys carry.
# The 30-day self-serve cap is lifted only for accounts a reviewer has granted.

def _active_grant(user) -> SurveyTokenGrant | None:
    if user is None or not getattr(user, "pk", None):
        return None
    return SurveyTokenGrant.objects.filter(user=user, revoked_at__isnull=True).first()


def can_issue_long_tokens(user) -> bool:
    """Whether ``user`` has an active grant to mint long-lived survey tokens."""
    return _active_grant(user) is not None


def user_token_cap(user) -> int:
    """The longest API token ``user`` may mint, in seconds.

    An active grant lifts the default 30-day ceiling to the grant's own
    ``max_ttl_seconds`` (or the configured survey maximum); everyone else stays
    at ``LUMINA_API_TOKEN_TTL_SECONDS``.
    """
    grant = _active_grant(user)
    if grant is None:
        return settings.LUMINA_API_TOKEN_TTL_SECONDS
    return grant.max_ttl_seconds or settings.LUMINA_SURVEY_MAX_TOKEN_TTL_SECONDS


def request_long_token(*, requester, justification, requested_ttl_seconds=None
                       ) -> SurveyTokenRequest:
    """Open a request for the long-lived-survey-token capability, judged by a reviewer.

    One open request per account, enforced here (the model's partial unique
    constraint is a no-op on MariaDB, as with vendor claims), inside a
    transaction with ``select_for_update`` so two simultaneous submits cannot
    both pass the check.
    """
    if not requester.is_authenticated:
        raise PermissionError("Authentication required to request survey tokens.")
    with transaction.atomic():
        already = (
            SurveyTokenRequest.objects.select_for_update()
            .filter(requester=requester, status__in=SurveyTokenRequest.OPEN_STATUSES)
            .exists()
        )
        if already:
            raise ValueError(
                "You already have an open survey token request. A reviewer will get to it."
            )
        req = SurveyTokenRequest.objects.create(
            requester=requester,
            justification=justification,
            requested_ttl_seconds=requested_ttl_seconds,
        )
    log_action("survey_token_request.submit", target=req, actor=requester)
    emit("survey_token_request.submitted", target=req, actor=requester)
    return req


def moderate_submission(submission: SurveySubmission, *, by, dismiss: bool) -> SurveySubmission:
    """A reviewer's moderation of one survey submission - accept (keep) or dismiss (exclude).

    Never a gate: a submission counts while ``new`` or ``accepted``; only a
    dismissal removes it from the rollup. Records who decided, for the audit trail.
    """
    submission.review_state = (
        SurveySubmission.REVIEW_DISMISSED if dismiss else SurveySubmission.REVIEW_ACCEPTED
    )
    submission.reviewed_by = by
    submission.reviewed_at = timezone.now()
    submission.save(update_fields=["review_state", "reviewed_by", "reviewed_at"])
    return submission
