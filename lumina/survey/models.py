"""The hardware survey: an append-only census of what AlmaLinux runs on.

A survey record is one machine's inventory at one moment, kept verbatim and
forever. Two principles from the design shape these tables:

*Store raw, decide later.* ``SurveySubmission`` is append-only - the raw payload
and raw identity are never overwritten. Dedup, period bucketing, and tier
weighting all happen downstream in ``SurveyStat``, recomputed by the
``survey_rollup`` job, so nothing here collapses. A parsing fix or a better idea
next year is re-derivable against data already kept.

*Aggregate out, never per-machine.* Raw identity (SMBIOS UUID, serials) lives on
``SurveySubmission`` in an access-controlled tier and is never serialized to the
public; only ``SurveyStat`` rollups are ever shown.

Firewalled from the certification catalog on purpose: everything here is a
normalized string, never a ``ForeignKey`` into ``lumina.hardware``. The survey
shares the collector, never the catalog.
"""
from __future__ import annotations

import uuid
from typing import override

from django.conf import settings
from django.db import models

from lumina.core.review import ReviewWorkflow


class SurveySubmissionQuerySet(models.QuerySet["SurveySubmission"]):
    def bare_metal(self) -> SurveySubmissionQuerySet:
        """Physical machines only. The survey is bare-metal; VM runs are kept but never counted."""
        return self.filter(virtual=False)

    def countable(self) -> SurveySubmissionQuerySet:
        """Rows that may enter a published statistic: bare metal, not moderated away."""
        return self.filter(virtual=False).exclude(
            review_state=self.model.REVIEW_DISMISSED
        )

    def pending_review(self) -> SurveySubmissionQuerySet:
        """Submissions no reviewer has looked at yet - the survey moderation queue.

        Standalone survey runs only. A cert-run fork is a byproduct of a validate or
        benchmark run that is *already* reviewable in the runs queue, so queueing it
        here a second time would ask a reviewer to moderate the same machine twice and
        would bury the standalone submissions this queue exists for. Those forks stay
        countable and stay dismissible from the admin; they just never demand attention.
        """
        return self.filter(
            review_state=self.model.REVIEW_NEW, origin=self.model.ORIGIN_SURVEY,
        )


class SurveySubmission(models.Model):
    """One inventory snapshot from one machine - append-only, retained indefinitely.

    The raw payload (``inventory`` and the raw identity columns) is immutable
    after creation. Only the operational columns - the moderation ``review_state``
    and the *derived* ``identity_hash``/``identity_source`` - may be updated
    later, and only through a ``save(update_fields=...)`` naming just those. A
    normalization change is applied by re-deriving at rollup time from the
    ``inventory`` that is kept forever, never by mutating a stored row.
    """

    # Where the record came from.
    ORIGIN_SURVEY = "survey"        # a standalone `alma-cert survey` run
    ORIGIN_CERT_RUN = "cert-run"    # forked from a validate/benchmark ingest
    ORIGIN_CHOICES = [
        (ORIGIN_SURVEY, "Survey run"),
        (ORIGIN_CERT_RUN, "Cert-run fork"),
    ]

    # How much the submission is trusted. Verified is account-backed and headlines
    # published stats; community (a future anonymous tier) is supplementary.
    TIER_VERIFIED = "verified"
    TIER_COMMUNITY = "community"
    TIER_CHOICES = [
        (TIER_VERIFIED, "Verified"),
        (TIER_COMMUNITY, "Community"),
    ]

    # Review state. Reviewable in its own queue, but it never blocks: a submission
    # counts in stats while `new` or `accepted`, and only a dismissal excludes it.
    # No submitter interaction is ever needed after the API call - a reviewer only
    # moderates, and even that is optional oversight rather than a gate.
    REVIEW_NEW = "new"
    REVIEW_ACCEPTED = "accepted"
    REVIEW_DISMISSED = "dismissed"
    REVIEW_CHOICES = [
        (REVIEW_NEW, "New"),
        (REVIEW_ACCEPTED, "Accepted"),
        (REVIEW_DISMISSED, "Dismissed"),
    ]

    # Columns an update may touch after creation. Everything else is raw and immutable.
    _MUTABLE_FIELDS = frozenset({
        "review_state", "reviewed_by", "reviewed_at", "identity_hash", "identity_source",
    })

    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    received_at = models.DateTimeField(auto_now_add=True, db_index=True)

    origin = models.CharField(max_length=16, choices=ORIGIN_CHOICES, db_index=True)
    trust_tier = models.CharField(max_length=16, choices=TIER_CHOICES, db_index=True)
    submitter = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="survey_submissions",
    )
    # The token that carried the submission, if any. SET_NULL so revoking a token
    # never deletes the census data it contributed.
    token = models.ForeignKey(
        "accounts.ApiToken",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    source_ip_hash = models.CharField(max_length=64, blank=True)

    # --- access-controlled identity tier -------------------------------------
    # Raw, kept indefinitely, and NEVER serialized to the public. Dedup keys on
    # ``identity_hash`` (HMAC of the strongest non-bogus signal), which is derived
    # and recomputable from the raw fields below if the denylist or pepper changes.
    system_uuid = models.CharField(max_length=64, blank=True)
    system_serial = models.CharField(max_length=128, blank=True)
    board_serial = models.CharField(max_length=128, blank=True)
    machine_id = models.CharField(max_length=64, blank=True)
    identity_hash = models.CharField(max_length=64, blank=True, db_index=True)
    identity_source = models.CharField(
        max_length=16, blank=True,
        help_text="Which raw signal keyed identity_hash: smbios_uuid | board_serial | machine_id.",
    )

    # Bare-metal only. A VM run is recorded (nothing is thrown away) but flagged
    # so the rollup can exclude it.
    virtual = models.BooleanField(default=False, db_index=True)
    virt_kind = models.CharField(max_length=32, blank=True)

    # Verbatim survey payload.
    inventory = models.JSONField(default=dict)

    # --- denormalized facets (mirror TestRun; normalized strings, no catalog FK) ---
    cpu_model = models.CharField(max_length=200, blank=True, db_index=True)
    cpu_family = models.CharField(max_length=120, blank=True, db_index=True)
    cpu_vendor = models.CharField(max_length=80, blank=True, db_index=True)
    cpu_sockets = models.PositiveSmallIntegerField(null=True, blank=True, db_index=True)
    cpu_cores = models.PositiveIntegerField(null=True, blank=True)
    cpu_threads = models.PositiveIntegerField(null=True, blank=True)
    memory_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    memory_type = models.CharField(max_length=32, blank=True, db_index=True)
    gpu_vendor = models.CharField(max_length=80, blank=True, db_index=True)
    gpu_model = models.CharField(max_length=200, blank=True, db_index=True)
    board_vendor = models.CharField(max_length=120, blank=True, db_index=True)
    board_model = models.CharField(max_length=200, blank=True, db_index=True)
    arch = models.CharField(max_length=32, blank=True, db_index=True)
    # The distro's built ISA baseline (x86_64, x86_64_v2, aarch64, ...) - distinct
    # from x86_64_level, which is what the CPU itself supports (v2/v3/v4).
    x86_64_level = models.CharField(max_length=8, blank=True, db_index=True)
    kernel = models.CharField(max_length=120, blank=True)
    os_major = models.PositiveSmallIntegerField(null=True, blank=True, db_index=True)
    os_minor = models.PositiveSmallIntegerField(null=True, blank=True, db_index=True)

    review_state = models.CharField(
        max_length=12, choices=REVIEW_CHOICES, default=REVIEW_NEW, db_index=True
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_survey_submissions",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    objects = SurveySubmissionQuerySet.as_manager()

    class Meta:
        ordering = ["-received_at"]
        indexes = [
            # Rollup dedup groups by identity within a period; both columns together.
            models.Index(fields=["identity_hash", "received_at"]),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"survey {self.uuid} ({self.origin}/{self.trust_tier})"

    @override
    def save(self, *args, **kwargs):
        if self.pk is not None:
            update_fields = kwargs.get("update_fields")
            if update_fields is None or not set(update_fields).issubset(self._MUTABLE_FIELDS):
                raise ValueError(
                    "SurveySubmission raw data is append-only; after creation only "
                    f"{sorted(self._MUTABLE_FIELDS)} may change (via update_fields)."
                )
        super().save(*args, **kwargs)

    @property
    def is_unreviewed(self) -> bool:
        """Whether a reviewer can still accept or dismiss this submission.

        Deliberately broader than the ``pending_review`` queryset. That one is the
        *queue* - what a reviewer is asked to look at, standalone survey runs only -
        while this is whether the moderation buttons would do anything, which is true
        of a cert-run fork as well. A fork never demands attention, but a reviewer who
        spots a bogus one should be able to dismiss it where they found it.
        """
        return self.review_state == self.REVIEW_NEW


class SurveyStat(models.Model):
    """A published aggregate: how many machines fall in one bucket of one dimension.

    Derived and disposable - wholly recomputed from ``SurveySubmission`` by the
    ``survey_rollup`` job, which upserts on the unique key below. The only survey
    data ever shown publicly. No bucket is suppressed for being small: an
    aggregate dimension is not identifying even at a count of one.
    """

    # Not a facet: one row per period holding how many machines were counted in it. Every
    # other dimension totals only the machines that reported *that* facet, so none of them
    # is the machine count: a fleet with Arm in it has fewer machines under x86_64_level
    # than under cpu_vendor, and reading the headline figure off any one of them is wrong
    # by however many machines left that field blank. Excluded from the published
    # dimensions, so it is never rendered as a distribution.
    MACHINES_DIMENSION = "machines"
    MACHINES_BUCKET = "all"

    DEDUP_SMBIOS = "smbios_uuid"   # physical machines
    DEDUP_MACHINE = "machine_id"   # OS installs

    TIER_VERIFIED = "verified"     # the headline scope
    TIER_ALL = "all"               # verified + community

    # Which cohort this row counts. Blank is the whole fleet, which is why it is the
    # default: every row written before segments existed is a whole-fleet row and stays
    # correct. A segment's rows are a rollup of their own, not a slice of these.
    segment = models.CharField(max_length=80, blank=True, db_index=True)
    period = models.CharField(max_length=16, db_index=True, help_text='"2026" or "2026-09".')
    dimension = models.CharField(max_length=40, db_index=True, help_text="cpu_family, gpu_vendor, ...")
    bucket = models.CharField(max_length=200)
    count = models.PositiveIntegerField(default=0)
    dedup_key = models.CharField(max_length=16)
    tier_scope = models.CharField(max_length=16)
    computed_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["dimension", "-count"]
        constraints = [
            models.UniqueConstraint(
                fields=["segment", "period", "dimension", "bucket", "dedup_key",
                        "tier_scope"],
                name="survey_stat_unique_bucket",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.period} {self.dimension}={self.bucket}: {self.count}"


class SurveyTokenGrant(models.Model):
    """Permission for one account to mint long-lived, submit-scoped survey tokens.

    Created when a reviewer approves a ``SurveyTokenRequest``. The token-create
    form consults this to lift the default 30-day cap for the grantee alone;
    everyone else stays capped. There is no generic per-user capability model in
    the project, so this follows the ``VendorMembership`` precedent: an
    app-managed grant a reviewer action writes.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="survey_token_grant",
    )
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    granted_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    max_ttl_seconds = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Longest token this account may mint. Null uses LUMINA_SURVEY_MAX_TOKEN_TTL_SECONDS.",
    )

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"survey token grant for {self.user}"

    def is_active(self) -> bool:
        return self.revoked_at is None


class SurveyTokenRequest(ReviewWorkflow, models.Model):
    """A request to be allowed to mint long-lived survey tokens, judged by a reviewer.

    Mirrors ``VendorClaim``: a stated case a reviewer approves or rejects.
    ``approve()`` grants the capability; ``reject``/``request_changes`` are the
    shared workflow. The requester supplies a written justification, exactly the
    context a reviewer reads before deciding.
    """

    review_noun = "survey token request"

    requester = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="survey_token_requests",
    )
    justification = models.TextField(
        help_text="Why this account needs long-lived survey tokens (fleet size, automation, ...)."
    )
    requested_ttl_seconds = models.PositiveIntegerField(null=True, blank=True)

    # --- ReviewWorkflow columns (the mixin declares no fields; it only writes them) ---
    status = models.CharField(
        max_length=16, choices=ReviewWorkflow.STATUS_CHOICES,
        default=ReviewWorkflow.STATUS_PENDING, db_index=True,
    )
    reviewer_notes = models.TextField(blank=True)
    submitted_at = models.DateTimeField(auto_now_add=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_survey_token_requests",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-submitted_at"]
        constraints = [
            # One open request per user. Belt-only: MariaDB skips partial indexes, so
            # the service enforces it too (as VendorClaim does).
            models.UniqueConstraint(
                fields=["requester"],
                condition=models.Q(
                    status__in=[
                        ReviewWorkflow.STATUS_PENDING,
                        ReviewWorkflow.STATUS_NEEDS_CHANGES,
                    ]
                ),
                name="survey_token_request_one_open_per_user",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"survey token request by {self.requester} ({self.status})"

    def approve(self, *, by, max_ttl_seconds=None) -> SurveyTokenGrant:
        """Grant the capability and stamp the approval.

        The app-specific half of the review workflow: create or re-arm the
        grant, then record the approval through the shared stamp.
        """
        grant, _ = SurveyTokenGrant.objects.update_or_create(
            user=self.requester,
            defaults={
                "granted_by": by,
                "revoked_at": None,
                "max_ttl_seconds": max_ttl_seconds,
            },
        )
        self._record_approval(by=by)
        return grant


class SurveySegment(models.Model):
    """A named subset of surveyed machines, with the whole page recomputed inside it.

    "Arm servers", "machines with a discrete GPU", "dual socket": a question about part
    of the fleet rather than all of it. A segment is not a filter applied to published
    percentages, which would be meaningless (a share of a share). It is its own rollup:
    the criteria narrow the submissions first, dedup runs inside the narrowed set, and
    every dimension is recounted, so shares add to 100% of *that* cohort.

    Admin-curated on purpose. Segments cost rollup time (one pass per segment per
    period), and a public page of arbitrary visitor-defined cohorts is a query amplifier
    pointed at the database.
    """

    # Criteria are validated against this allowlist rather than passed to ``filter``
    # as-is. The fields are the published facets and nothing else: without the
    # allowlist an admin typo could reach a related model, and a segment on
    # ``submitter__*`` would quietly make a public page that says something about one
    # person's machines.
    SEGMENTABLE_FIELDS = {
        "arch", "x86_64_level", "cpu_vendor", "cpu_model", "cpu_family", "cpu_sockets",
        "cpu_cores", "cpu_threads", "memory_bytes", "memory_type", "gpu_vendor",
        "gpu_model", "board_vendor", "board_model", "kernel", "os_major", "os_minor",
        "origin", "trust_tier",
    }
    # Enough to express a cohort, and nothing that can run away: no regex, no traversal.
    OPERATORS = {
        "eq": "exact", "ne": "exact", "in": "in", "contains": "icontains",
        "gt": "gt", "gte": "gte", "lt": "lt", "lte": "lte",
        "isnull": "isnull", "not_blank": "exact",
    }
    NEGATED = {"ne", "not_blank"}

    name = models.CharField(max_length=80, unique=True)
    slug = models.SlugField(max_length=80, unique=True)
    description = models.CharField(
        max_length=300, blank=True,
        help_text="Shown on the statistics page, so say what the cohort is.",
    )
    # A list of {"field": ..., "op": ..., "value": ...}. All clauses must match.
    criteria = models.JSONField(
        default=list,
        help_text=(
            'Clauses, all of which must match: '
            '[{"field": "arch", "op": "eq", "value": "aarch64"}]. '
            'Operators: eq, ne, in, contains, gt, gte, lt, lte, isnull, not_blank.'
        ),
    )
    enabled = models.BooleanField(
        default=True,
        help_text="Disabled segments stay in the rollup but are not offered on the page.",
    )
    position = models.PositiveSmallIntegerField(
        default=0, help_text="Order in the picker; ties fall back to the name."
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["position", "name"]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.name

    def clean(self):
        """Reject criteria the rollup could not apply, at the point somebody types them.

        A segment with a bad clause would otherwise fail in the nightly rollup, where
        nobody is watching and the only symptom is a cohort that never has any data.
        """
        from django.core.exceptions import ValidationError

        if not isinstance(self.criteria, list) or not self.criteria:
            raise ValidationError({"criteria": "Give at least one clause, as a list."})
        for clause in self.criteria:
            if not isinstance(clause, dict):
                raise ValidationError({"criteria": f"Not a clause: {clause!r}."})
            field, op = clause.get("field"), clause.get("op", "eq")
            if field not in self.SEGMENTABLE_FIELDS:
                raise ValidationError({"criteria": (
                    f"{field!r} is not a segmentable field. One of: "
                    f"{', '.join(sorted(self.SEGMENTABLE_FIELDS))}."
                )})
            if op not in self.OPERATORS:
                raise ValidationError({"criteria": (
                    f"{op!r} is not an operator. One of: "
                    f"{', '.join(sorted(self.OPERATORS))}."
                )})
            if op == "in" and not isinstance(clause.get("value"), list):
                raise ValidationError({"criteria": '"in" needs a list value.'})

    def narrow(self, queryset):
        """Apply this segment to a submission queryset.

        Clauses are ANDed. ``not_blank`` is the common case of "reported this at all",
        which is a negated match on the empty string rather than a null check, because
        the facet columns are blank-not-null.
        """
        for clause in self.criteria or []:
            field = clause["field"]
            op = clause.get("op", "eq")
            lookup = self.OPERATORS[op]
            value = "" if op == "not_blank" else clause.get("value")
            condition = {f"{field}__{lookup}": value}
            queryset = (
                queryset.exclude(**condition) if op in self.NEGATED
                else queryset.filter(**condition)
            )
        return queryset
