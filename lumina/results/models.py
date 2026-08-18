"""Ingested certification-suite results.

A ``TestRun`` is one execution of the alma-cert suite (collect, validate, or
benchmark) submitted either through the API or the manual upload form. The
verbatim ``report.json`` inventory is kept in a JSONField; the handful of
fields that leaderboards, statistics, and filters query are denormalized into
indexed columns because MariaDB cannot index JSON paths without generated
columns.

Visibility: everything public goes through ``TestRun.objects.public()``.
Embargoed runs (pre-release hardware with a future publish date) are absent
from it entirely - there is deliberately no "exists but hidden" placeholder
that would reveal unreleased hardware.
"""
from __future__ import annotations

import uuid as uuid_lib
from enum import StrEnum
from pathlib import Path

from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils import timezone


class RunType(StrEnum):
    collect = "collect"
    validate = "validate"
    benchmark = "benchmark"


class TargetType(StrEnum):
    hardware = "hardware"
    cloud_instance = "cloud_instance"


class RunSource(StrEnum):
    api = "api"
    web_upload = "web_upload"


class SystemKind(models.TextChoices):
    """Whether the machine is a vendor system model or a self-built box.

    A prebuilt (Dell R720, HP DL380 G10) has a real vendor model in the DMI
    system table; on a custom build that table is placeholder text or a
    mirror of the motherboard, and the machine's real identity is its
    components (board + CPU).

    **Two kinds, and custom is the fallback.** There was a third, "unknown", for a machine whose
    firmware named neither a system nor a board maker. It is gone: a machine is claimed to be a
    vendor-built system or it is not, and "not" is a custom build. Nothing is lost by dropping it,
    because the thing "unknown" actually protected against was creating a listing with no usable
    identity - and ``create_listings_from_run`` already refuses that on its own terms, with a
    message naming the missing field rather than the classification.

    Classified at *ingest*, by ``inventory_extract.system_kind``, from the strings the report
    carries. The collector used to do it and write the answer into the bundle - which froze a
    guess about what firmware authors meant into every bundle ever written, and left 1.0 reports
    unclassified because no classifier existed when they were made. The verbatim DMI stays in
    ``inventory`` either way, and a reviewer's correction still wins
    (``effective_system_kind``).
    """

    PREBUILT = "prebuilt", "Prebuilt"
    CUSTOM = "custom", "Custom build"


class ResultStatus(models.TextChoices):
    PASS = "pass", "Pass"
    FAIL = "fail", "Fail"
    SKIP = "skip", "Skip"
    ERROR = "error", "Error"


class Severity(models.TextChoices):
    REQUIRED = "required", "Required"
    CONDITIONAL = "conditional", "Conditional"
    INFORMATIONAL = "informational", "Informational"


class MetricDirection(models.TextChoices):
    HIGHER = "higher_is_better", "Higher is better"
    LOWER = "lower_is_better", "Lower is better"
    INFO = "info", "Informational"


def _bundle_upload_to(instance: TestRun, filename: str) -> str:
    ext = ".tar.gz" if filename.endswith(".tar.gz") else ".tar.zst"
    return str(Path("test-runs") / str(instance.uuid) / f"bundle{ext}")


def _artifact_upload_to(instance: RunArtifact, filename: str) -> str:
    return str(Path("test-runs") / str(instance.run.uuid) / "artifacts" / filename)


class TestRunQuerySet(models.QuerySet["TestRun"]):
    def public(self) -> TestRunQuerySet:
        """Approved and past its publish date. The only queryset public
        views and the read API may use."""
        return self.filter(
            status=TestRun.STATUS_APPROVED,
            published_at__isnull=False,
            published_at__lte=timezone.now(),
        )

    def archived(self) -> TestRunQuerySet:
        return self.exclude(archived_at=None)

    def active(self) -> TestRunQuerySet:
        """Everything the submitter has not put away. The dashboard's default."""
        return self.filter(archived_at=None)

    def with_benchmarks(self) -> TestRunQuerySet:
        """Runs that carry benchmark metrics, whatever they are filed under.

        ``run_type`` is a single column and ``run.run_types`` in the report is a
        list, so ``_primary_run_type`` collapses it: a combined ``alma-cert run``
        is filed as *validate*, because validation outranks benchmarking for
        review purposes. Asking for ``run_type="benchmark"`` therefore misses
        every combined run, and its metrics sit in the database invisible to the
        landing feed and the benchmark feed alike. Ask what a run carries.
        """
        from django.db.models import Exists, OuterRef

        return self.filter(Exists(BenchmarkResult.objects.filter(run=OuterRef("pk"))))

    def open_for_review(self) -> TestRunQuerySet:
        """Runs a reviewer should act on. Drafts are excluded: they are
        waiting on their submitter, not on a reviewer."""
        return self.filter(status__in=TestRun.OPEN_STATUSES)

    def quarantined(self) -> TestRunQuerySet:
        """Runs held because their report says they were not run on AlmaLinux.

        A separate queue from ``open_for_review``. Mixing them in would put a
        Rocky run beside genuine AlmaLinux evidence with only a badge to tell them
        apart, and the decision is a different one: not "is this hardware
        certified" but "is this report's operating system wrong".
        """
        return self.filter(status=TestRun.STATUS_QUARANTINED)

    def drafts_for(self, user) -> TestRunQuerySet:
        return self.filter(submitter=user, status=TestRun.STATUS_DRAFT)

    def awaiting_submitter(self, user) -> TestRunQuerySet:
        """Runs where the next move is this user's, and they have not said otherwise.

        Defined here rather than in the dashboard, because ``SUBMITTER_STATUSES`` and the reason
        for it live in this file and a second, subtly different definition next to the view is how
        the two would drift about what "waiting on the submitter" means.

        Archived runs are excluded, and that is not a detail. Archiving is a statement that the
        submitter does not intend to act, which is the exact negation of a call to action; without
        this clause everything they deliberately put away reappears at the top of the page.
        """
        return self.filter(
            submitter=user, status__in=TestRun.SUBMITTER_STATUSES, archived_at=None,
        )

    def visible_to(self, user) -> TestRunQuerySet:
        """Public runs, plus the user's own, plus everything for reviewers."""
        if not getattr(user, "is_authenticated", False):
            return self.public()
        from lumina.review.permissions import is_reviewer

        if is_reviewer(user):
            return self
        return self.filter(
            models.Q(submitter=user)
            | models.Q(
                status=TestRun.STATUS_APPROVED,
                published_at__isnull=False,
                published_at__lte=timezone.now(),
            )
        )


class TestRun(models.Model):
    # A validation run arrives incomplete on purpose: the suite cannot know
    # the marketing name, the description, or the spec-sheet link that make a
    # good catalog listing, so the submitter completes it on the web and then
    # releases it for review. Benchmark and collect runs need nothing extra
    # and go straight to pending.
    STATUS_DRAFT = "draft"
    STATUS_PENDING = "pending"
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"
    STATUS_NEEDS_CHANGES = "needs-changes"
    # A run whose report says it was not performed on AlmaLinux. Stored rather
    # than refused at the door, so an attempted submission is visible instead of
    # merely bounced, but held outside every path that could turn it into a
    # certification: it is not an OPEN_STATUS, so ``_require_open`` refuses to
    # approve it, and ``public()`` filters on APPROVED so it can never be public.
    # A reviewer who can see that the OS was misreported releases it explicitly.
    STATUS_QUARANTINED = "quarantined"
    STATUS_CHOICES = [
        (STATUS_DRAFT, "Awaiting submitter details"),
        (STATUS_PENDING, "Pending"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_REJECTED, "Rejected"),
        (STATUS_NEEDS_CHANGES, "Needs changes"),
        (STATUS_QUARANTINED, "Quarantined: not run on AlmaLinux"),
    ]
    OPEN_STATUSES = (STATUS_PENDING, STATUS_NEEDS_CHANGES)
    # Drafts are the submitter's to finish; they are not in anyone's queue.
    SUBMITTER_STATUSES = (STATUS_DRAFT, STATUS_NEEDS_CHANGES)
    # What a submitter may put out of sight on their own dashboard.
    #
    # The rule is "the ball is with me and I do not intend to play it". A draft nobody finished, a
    # run sent back that will not be fixed, one that was turned down, one that turns out to have
    # been run on Rocky: all of those sit on the dashboard forever otherwise, and a workspace full
    # of things the reader has already decided about is a workspace they stop reading.
    #
    # Not ``pending``: it is in a reviewer's queue right now, and letting a submitter hide their
    # own half of a conversation somebody else is in the middle of is how a queue item becomes
    # untraceable from the submitter's side. Withdrawing is a different action with a different
    # name, and nobody has asked for it.
    #
    # Not ``approved``: it is evidence. An approved run backs an attestation and appears in the
    # public catalog, and hiding it from the person who submitted it would mean the dashboard no
    # longer answers "what have I certified".
    ARCHIVABLE_STATUSES = (
        STATUS_DRAFT, STATUS_NEEDS_CHANGES, STATUS_REJECTED, STATUS_QUARANTINED,
    )

    RUN_TYPE_CHOICES = [(t.value, t.value.capitalize()) for t in RunType]
    TARGET_TYPE_CHOICES = [
        (TargetType.hardware.value, "Hardware"),
        (TargetType.cloud_instance.value, "Cloud instance"),
    ]
    SOURCE_CHOICES = [
        (RunSource.api.value, "API"),
        (RunSource.web_upload.value, "Web upload"),
    ]

    uuid = models.UUIDField(default=uuid_lib.uuid4, unique=True, editable=False)
    run_type = models.CharField(max_length=16, choices=RUN_TYPE_CHOICES, db_index=True)
    # The component kinds this run is a *claim about*. Empty means the whole machine, which is
    # every run written before this existed and most runs after it.
    #
    # Asked for so a GPU can be validated on its own: an NVIDIA L40S passed through to a cloud
    # instance is the card the vendor wants certified, and the instance around it is a rented
    # hypervisor guest nobody should certify anything about. CPUs work the same way, being handed
    # to the guest directly.
    #
    # Deliberately not folded into ``target_type``, which answers "what kind of host was this run
    # on". The two are independent and both combinations are real: a card in a lab server is a
    # GPU-scoped run on bare metal, and certifying a cloud image is a whole-machine run of a guest.
    #
    # A list because "the GPU and the CPU, on one instance" is the obvious next request and costs
    # nothing to allow. Values are ``hardware.ComponentKind`` names; ingest rejects anything else,
    # because a scope nobody can interpret would be a claim of unknown size.
    claim_scope = models.JSONField(
        default=list, blank=True,
        help_text=(
            "Component kinds this run is evidence for. Empty means the whole machine. A scoped "
            "run can never certify a System, whatever else it reports."
        ),
    )
    target_type = models.CharField(
        max_length=20, choices=TARGET_TYPE_CHOICES, default=TargetType.hardware.value
    )
    schema_version = models.CharField(max_length=16)
    suite_version = models.CharField(max_length=40)
    suite_git_commit = models.CharField(max_length=40, blank=True)

    submitter = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="test_runs"
    )
    source = models.CharField(max_length=16, choices=SOURCE_CHOICES)

    # Nullable at ingest; a reviewer links the run to catalog listings.
    # Prebuilt machines link to a System; custom builds have no vendor
    # system model, so their runs link to the Components they exercise
    # (motherboard, CPU, ...).
    listing_system = models.ForeignKey(
        "hardware.System",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="test_runs",
    )
    listing_components = models.ManyToManyField(
        "hardware.Component",
        blank=True,
        related_name="test_runs",
    )
    claimed_validation_level = models.CharField(max_length=16, blank=True)
    # The vendor this run is submitted for. Mirrors Submission.on_behalf_of:
    # a verified vendor's own member validating that vendor's hardware is what
    # makes a run vendor-validated, and it becomes the listing's owner_vendor
    # so they can maintain it afterwards.
    on_behalf_of = models.ForeignKey(
        "vendors.Vendor",
        null=True, blank=True, on_delete=models.SET_NULL,
        related_name="test_runs",
        help_text="Vendor this run is submitted on behalf of, when the "
                  "submitter is one of their members.",
    )
    # Submitter-proposed catalog listing details, filled in from the run's
    # own inventory and edited by the submitter (a validation run alone
    # doesn't carry enough to make a good listing: description, spec URL,
    # marketing name). Consumed at approval; shape:
    # {"vendor_name", "name", "model_number", "description", "vendor_spec_url"}
    listing_proposal = models.JSONField(null=True, blank=True)
    # Component ties the submitter (or a reviewer) has said not to make, as the tie keys
    # ``results.services.tie_key`` produces.
    #
    # A model field rather than another key in ``listing_proposal`` because it has to
    # survive two things that blob does not: ``merge_listing_proposal`` keeps only the
    # keys present in the incoming save plus the release ticks, and the reviewer's
    # assign-listing path never touches the proposal at all.
    #
    # It exists because removal did not work for anybody. ``ensure_component_ties``
    # re-derives every tie from the report at approval time, so a reviewer clearing the
    # component list watched all of it come back: measured at 0 components after clearing
    # and 3 again after approving.
    excluded_component_ties = models.JSONField(default=list, blank=True)
    # Corrections to what the report said a component is, as
    # ``{tie_key: {"brand": ..., "model": ...}}``. Either key may be absent or blank, which
    # means "keep what was reported" rather than "blank it".
    #
    # DMI and lspci are often wrong or unhelpful, and only somebody holding the machine - or
    # a reviewer looking at the whole submission - can say that "OEM" is really ASRock or
    # that "CometLake-S GT2 [UHD Graphics 630]" is a UHD Graphics 630. Left uncorrected a bad
    # vendor string becomes a catalog manufacturer named after it.
    component_overrides = models.JSONField(default=dict, blank=True)
    # The submitter says the catalog entry this run was matched to is not this machine.
    #
    # Identity matching is a heuristic over firmware strings, and two different machines can
    # report the same vendor and model - a rebadge, a barebones chassis sold by several
    # integrators, or simply a DMI table nobody filled in. Without a way to say so, a run
    # auto-linked at ingest was stuck attesting somebody else's listing, and the identity
    # fields that would let the submitter describe the real machine are hidden precisely
    # because the listing is not theirs.
    #
    # Set, this makes the run behave as new hardware throughout: ``existing_listing_for``
    # reports nothing, the identity fields come back, and approval creates a listing instead
    # of reusing the match.
    identity_disputed = models.BooleanField(default=False)

    alma_release = models.ForeignKey(
        "releases.AlmaLinuxRelease",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="test_runs",
    )
    alma_minor = models.PositiveSmallIntegerField(null=True, blank=True)

    # ``environment.os.id`` from the report: "almalinux", "rocky", "rhel", ...
    #
    # Denormalized out of the JSON deliberately, the same way ``cpu_model`` is.
    # It decides whether a run may certify anything, so the review queue and the
    # admin need to filter and display it without a JSON traversal per row.
    # Blank on runs ingested before this field existed - see
    # ``0013_quarantine_unsupported_os``, which backfills from the stored
    # environment rather than leaving them ambiguous.
    host_os_id = models.CharField(max_length=32, blank=True, db_index=True)

    # Set when a reviewer decides the reported OS was wrong - a rebuilt image, a
    # container that lost its os-release - and releases the run from quarantine.
    #
    # It exists so the OS gate does not live only in ``status``. Without it,
    # editing a quarantined run's status in the admin would be enough to make it
    # approvable, and it would then be published as an AlmaLinux validation on the
    # strength of a report that says otherwise.
    os_quarantine_released = models.BooleanField(default=False)

    # Verbatim inventory from report.json.
    inventory = models.JSONField(default=dict)
    environment = models.JSONField(default=dict)

    # Denormalized for filtering, faceting, and statistics.
    cpu_model = models.CharField(max_length=200, blank=True, db_index=True)
    cpu_vendor = models.CharField(max_length=80, blank=True, db_index=True)
    cpu_cores = models.PositiveIntegerField(null=True, blank=True)
    # Physical processors. An all-core score is not comparable without it: two
    # sockets of the same part roughly double the multi-core result, so a
    # 2P machine ranked against a 1P machine on cpu_model alone is a
    # meaningless comparison. Indexed because it is a leaderboard facet.
    cpu_sockets = models.PositiveSmallIntegerField(null=True, blank=True,
                                                   db_index=True)
    cpu_threads = models.PositiveIntegerField(null=True, blank=True)
    memory_mb = models.PositiveIntegerField(null=True, blank=True)
    # DIMM topology, summarized from the per-module detail kept in inventory.
    # How memory is populated changes bandwidth as much as how much there is:
    # eight single-rank DDR4-2400 modules and two DDR4-3200 are not the same
    # machine even at equal capacity.
    memory_dimm_count = models.PositiveSmallIntegerField(null=True, blank=True)
    memory_type = models.CharField(max_length=32, blank=True, db_index=True)
    memory_speed_mts = models.PositiveIntegerField(null=True, blank=True,
                                                   db_index=True)
    gpu_model = models.CharField(max_length=200, blank=True, db_index=True)
    gpu_driver = models.CharField(max_length=120, blank=True, db_index=True)
    system_kind = models.CharField(
        max_length=10, choices=SystemKind.choices, default=SystemKind.CUSTOM,
        db_index=True,
    )
    system_vendor = models.CharField(max_length=120, blank=True, db_index=True)
    system_product = models.CharField(max_length=200, blank=True)
    # The vendor's own machine-type code, when DMI carries one separately from
    # the readable model: Lenovo reports "21K9001NUS" alongside its product
    # name. Prefills the listing so a submitter does not retype it.
    system_model_number = models.CharField(max_length=120, blank=True)
    board_vendor = models.CharField(max_length=120, blank=True, db_index=True)
    board_model = models.CharField(max_length=200, blank=True, db_index=True)

    bundle = models.FileField(upload_to=_bundle_upload_to, max_length=300)
    bundle_sha256 = models.CharField(max_length=64, db_index=True)
    bundle_size = models.PositiveBigIntegerField(default=0)
    # The report's own canonical self-hash. Duplicate detection compares this
    # rather than the bundle bytes: re-tarring an unchanged run can produce a
    # different archive, but the report hash only changes with the content.
    report_sha256 = models.CharField(max_length=64, blank=True, db_index=True)

    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True
    )
    reviewer_notes = models.TextField(blank=True)
    submitter_notes = models.TextField(blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_test_runs",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    # --- the two gates -------------------------------------------------------------
    #
    # Both hold something back from the public, for unrelated reasons, and they compose: a
    # Kitten run on unreleased hardware is gated twice and each lifts on its own schedule.
    #
    # ``pre_release`` is about *secrecy*. The hardware is not announced, so nothing it certifies
    # may appear until a date the submitter names.
    #
    # ``available_from_minor`` is about *timing*. The hardware is public and the evidence is
    # real, but it was proved on AlmaLinux Kitten and the enablement lands in an upcoming minor,
    # so the listing carries a disclaimer until that minor ships. The entry is published either
    # way - a reader looking for this machine is better served by "works from 10.3" than by
    # nothing at all.
    pre_release = models.BooleanField(default=False)
    publish_requested_date = models.DateField(null=True, blank=True)
    available_from_minor = models.PositiveSmallIntegerField(
        null=True, blank=True,
        help_text=(
            "The minor of this run's major in which the hardware enablement lands. Set only "
            "for a run on AlmaLinux Kitten, where the major is proved but the support is not "
            "in a shipped minor yet. Null means nothing to wait for."
        ),
    )
    published_at = models.DateTimeField(null=True, blank=True, db_index=True)
    # When the submitter put this out of sight, or null. A timestamp rather than a flag, for the
    # same reason ``published_at`` is one: "when" answers "whether" and nothing answers "when".
    #
    # Purely a fact about one person's dashboard. Nothing archivable can be public - ``public()``
    # filters on ``approved`` and no archivable status is approved - and nothing archivable is in
    # the review queue, so this column is deliberately read by exactly one page.
    archived_at = models.DateTimeField(null=True, blank=True, db_index=True)

    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(auto_now_add=True, db_index=True)

    objects = TestRunQuerySet.as_manager()

    class Meta:
        ordering = ["-received_at"]
        indexes = [
            models.Index(fields=["run_type", "status"]),
            models.Index(fields=["run_type", "published_at"]),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.run_type} run {self.uuid} ({self.status})"

    def get_absolute_url(self) -> str:
        return reverse("results:run_detail", args=[self.uuid])

    # --- derived state ------------------------------------------------------
    @property
    def is_public(self) -> bool:
        return (
            self.status == self.STATUS_APPROVED
            and self.published_at is not None
            and self.published_at <= timezone.now()
        )

    @property
    def is_embargoed(self) -> bool:
        """Approved but deliberately withheld until publish_requested_date."""
        return self.status == self.STATUS_APPROVED and self.published_at is None

    @property
    def is_embargoed_by_request(self) -> bool:
        """Whether the submitter's own answers still ask for this run to be withheld.

        Distinct from ``is_embargoed``, which is the *state* - approved and not yet published.
        This is the *request*, and a reviewer editing it needs the two apart: clearing the tick
        is what ends a hold that has no date to end on its own.
        """
        if not self.pre_release:
            return False
        if self.publish_requested_date is None:
            return True
        from django.utils import timezone

        return self.publish_requested_date > timezone.localdate()

    @property
    def is_scoped(self) -> bool:
        """Whether this run is a claim about particular components rather than a machine."""
        return bool(self.claim_scope)

    @property
    def scope_labels(self) -> list[str]:
        """The scope in words, for a sentence a reader can act on."""
        from lumina.hardware.models import ComponentKind

        labels = {
            ComponentKind.cpu.value: "CPU",
            ComponentKind.gpu.value: "GPU",
            ComponentKind.nic.value: "network adapter",
            ComponentKind.storage.value: "storage device",
            ComponentKind.motherboard.value: "motherboard",
            ComponentKind.management.value: "management controller",
        }
        return [labels.get(kind, kind) for kind in self.claim_scope]

    @property
    def is_quarantined(self) -> bool:
        return self.status == self.STATUS_QUARANTINED

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None

    @property
    def can_archive(self) -> bool:
        """Whether the submitter may put this out of sight. See ``ARCHIVABLE_STATUSES``."""
        return not self.is_archived and self.status in self.ARCHIVABLE_STATUSES

    @property
    def ran_on_almalinux(self) -> bool:
        """What the report says, with no interpretation."""
        from lumina.results import inventory_extract

        return self.host_os_id == inventory_extract.ALMALINUX_OS_ID

    @property
    def may_certify_almalinux(self) -> bool:
        """Whether this run is allowed to become AlmaLinux certification evidence.

        Two ways to qualify: the report says AlmaLinux, or a reviewer has said the
        report was wrong. Checked independently of ``status`` on purpose, so the
        gate survives someone editing the status by hand.

        Blank ``host_os_id`` - runs ingested before the field existed - is treated
        as AlmaLinux, because at the time nothing else could be submitted: the
        suite had no other path and the backfill migration sets the real value
        wherever the stored environment records one.
        """
        return (
            self.host_os_id == "" or self.ran_on_almalinux
            or self.os_quarantine_released
        )

    CPU_FLAGS_TEST_ID = "validate.cpu.flags"

    @property
    def cpu_flags(self) -> list[str]:
        """The CPU's advertised feature flags, from the collected inventory.

        The inventory rather than the ``validate.cpu.flags`` result, because the
        inventory is always there: a ``collect`` run has no validation results at
        all, and a run from a suite older than that test still carries the flags.

        Already sorted and de-duplicated by the suite (its ``docs/schema.md``
        guarantees it), so two runs of one CPU are diffable. Not re-sorted here -
        doing so would hide a suite that stopped honouring that.

        The path is ``inventory.summary.cpus[0].flags``: ``collect_all`` wraps the
        normalized summary alongside a map of raw artifact paths, so the fields are
        one level down from the top of ``inventory``.
        """
        from lumina.results import inventory_extract

        flags = inventory_extract.first_cpu(self.inventory).get("flags") or []
        return [str(flag) for flag in flags]

    @property
    def cpu_flag_groups(self) -> dict:
        """The notable flags, grouped, as the suite classified them.

        Read off the informational result rather than re-derived here on purpose.
        Which flags are "notable" and how they group is an editorial judgement the
        suite already makes (``NOTABLE_FLAGS`` in ``almacert/validate/cpu.py``);
        a second copy in this repo would drift from it silently, and neither copy
        would be wrong enough to notice.

        Empty for a run with no such result - an older suite, or a collect-only
        run. The full list is still available from ``cpu_flags``, so the display
        degrades to the plain list rather than to nothing.
        """
        result = next(
            (r for r in self.results.all() if r.test_id == self.CPU_FLAGS_TEST_ID),
            None,
        )
        groups = (result.details or {}).get("notable") if result else None
        if not isinstance(groups, dict):
            return {}
        # Only groups that actually name flags. The suite omits empty ones, but a
        # hand-built or future report might not, and an empty row would read as a
        # finding ("no confidential computing") rather than as absence.
        return {
            str(name): [str(flag) for flag in found]
            for name, found in groups.items()
            if found
        }

    @property
    def memory_gb(self) -> int | None:
        """Capacity in GB. Megabytes are the stored unit and not a readable one:
        "644345 MB" is a number nobody converts in their head."""
        if not self.memory_mb:
            return None
        return round(self.memory_mb / 1024)

    @property
    def memory_slots_total(self):
        """Total memory slots, populated or not. Room to expand is part of what
        a reader wants from a server; reported by DMI as one type-17 record per
        slot, empty ones included."""
        memory = ((self.inventory or {}).get("summary") or {}).get("memory") or {}
        return memory.get("slots_total")

    # How the catalog recognises AlmaLinux Kitten. Read from the stored string rather than
    # frozen into a column at ingest, because Kitten's naming is not ours to control: if it
    # changes, this is one line and every bundle already submitted is re-read correctly.
    PRERELEASE_OS_MARKER = "kitten"

    @property
    def os_pretty_name(self) -> str:
        """``PRETTY_NAME`` from ``/etc/os-release``, e.g. "AlmaLinux Kitten 10 (Purple Lion)"."""
        return ((self.environment or {}).get("os") or {}).get("pretty_name") or ""

    @property
    def ran_on_prerelease_os(self) -> bool:
        """Whether this run happened on AlmaLinux Kitten rather than a shipped release.

        Kitten is indistinguishable from the stable release of the same major on every other
        field: it reports ``ID=almalinux`` and ``VERSION_ID=10`` exactly as AlmaLinux 10 does,
        and names itself only in ``PRETTY_NAME``.

        It matters because the two prove different things. A pass on a shipped release proves
        the hardware works on what people can install today. A pass on Kitten proves the major
        works, but the enablement lands in an upcoming minor - so the listing says so until that
        minor ships.

        False for a run whose report predates the field, which is the right default: hardware is
        gated on evidence that it was pre-release, never on an absence.
        """
        return self.PRERELEASE_OS_MARKER in self.os_pretty_name.lower()

    @property
    def kernel(self) -> str:
        """The kernel the run happened on, from ``environment.os.kernel``.

        ``uname -r``, so "5.14.0-503.el9.x86_64". Read from the stored environment rather than
        denormalized: nothing filters or aggregates on it, and it is only ever wanted one run at
        a time on the page a reviewer is reading.

        Worth showing at all because "AlmaLinux 9.8" does not say which kernel proved the
        hardware. A machine that needs a driver shipped in a later kernel passes on one and
        fails on another, and the release floor a submitter claims is a statement about exactly
        that. A reviewer weighing whether the evidence supports "9.8+" is asking this question.

        Blank rather than "unknown" when absent, so the template can hide the row. Runs from
        before the suite recorded it have no answer, and a literal "unknown" reads as a finding.
        """
        kernel = ((self.environment or {}).get("os") or {}).get("kernel") or ""
        return "" if kernel.strip().lower() == "unknown" else kernel.strip()

    # ``/proc/sys/kernel/tainted``, bit by bit, per the kernel's own
    # Documentation/admin-guide/tainted-kernels.rst. Only the ones a reader can act on are
    # worded here; anything unmapped still shows as the raw value.
    #
    # Decoded rather than reduced to "tainted" because the reasons mean very different things to
    # somebody weighing the evidence. A proprietary module loaded says the pass may be about
    # that module; a recent oops says the machine was unwell during the run; live patching says
    # the running kernel is not what the version string names. Writing one explanation for all
    # of them gets it wrong: this run reports 4, which is an out-of-spec CPU and has nothing to
    # do with modules at all.
    TAINT_BITS = {
        0: "a proprietary module was loaded",
        1: "a module was force loaded",
        2: "the CPU is out of spec or unsupported",
        3: "a module was force unloaded",
        4: "a machine check exception occurred",
        5: "a bad page was referenced",
        6: "taint was requested from userspace",
        7: "the kernel died recently (oops or BUG)",
        8: "an ACPI table was overridden",
        9: "the kernel issued a warning",
        10: "a staging driver was loaded",
        11: "a firmware-bug workaround was applied",
        12: "an out-of-tree module was loaded",
        13: "an unsigned module was loaded",
        14: "a soft lockup occurred",
        15: "the kernel was live patched",
        16: "auxiliary taint, defined by the distribution",
        17: "the kernel was built with struct randomization",
        18: "an in-kernel test was run",
    }

    @property
    def kernel_taint(self) -> int:
        """``/proc/sys/kernel/tainted`` as reported, or 0."""
        try:
            return int((self.environment or {}).get("kernel_taint") or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def kernel_tainted(self) -> bool:
        return self.kernel_taint != 0

    @property
    def kernel_taint_reasons(self) -> list[str]:
        """Why the kernel is tainted, in words, highest-signal first is not attempted - the bit
        order is the kernel's and keeping it makes the list checkable against the raw value."""
        value = self.kernel_taint
        return [
            reason for bit, reason in self.TAINT_BITS.items() if value & (1 << bit)
        ]

    @property
    def nvidia_driver(self) -> dict:
        """``environment.nvidia_driver`` from the report, or an empty dict.

        Absent on every machine with no NVIDIA card, and on every run from a suite older than the
        field, so callers get an empty dict rather than having to know which.
        """
        value = (self.environment or {}).get("nvidia_driver")
        return value if isinstance(value, dict) else {}

    @property
    def driver_loaded_during_run(self) -> bool:
        """Whether the suite loaded the GPU driver itself rather than finding it up at boot.

        The one fact in that section that cannot be reconstructed afterwards: by the time anybody
        reads the report, the modules are loaded either way. It qualifies what a GPU pass is evidence
        of, in the same way a tainted kernel does, because a hot-loaded machine is running a
        configuration no boot has applied - the driver install writes ``rd.driver.blacklist=nouveau``
        to the kernel command line and regenerates the initramfs, and neither has been used yet.

        Read here rather than decided in the collector on purpose. The suite records what it did; how
        much that matters is this side's call, and changing our mind costs a deploy rather than a new
        release of the CLI on every certifying partner's machine.
        """
        return bool(self.nvidia_driver.get("loaded_by_alma_cert"))

    @property
    def driver_load_notes(self) -> list[str]:
        """What else the report says about a hot-loaded driver, for a reviewer weighing it.

        Only the facts that change how the result should be read, and only when they are true, so an
        ordinary hot load shows one line and an unusual one shows what made it unusual.

        Whole sentences, capitalized here rather than by the template. The kernel taint reasons next
        to these are run through ``capfirst``, which is right for prose and wrong the moment a
        sentence opens with a module name: it renders ``nvidia_uvm`` as ``Nvidia_uvm``, which is not
        the name of anything.
        """
        record = self.nvidia_driver
        notes = []
        if record.get("installed_during_run"):
            notes.append("The driver was installed during this run as well.")
        if record.get("install_failed_at"):
            notes.append(f"The install did not finish: {record['install_failed_at']}.")
        if record.get("newer_kernel_installed"):
            notes.append(
                "A newer kernel is installed, so the next boot is not the kernel these results "
                "came from."
            )
        modules = record.get("modules_after") or []
        if modules and "nvidia_uvm" not in modules:
            notes.append("nvidia_uvm was not loaded, and every CUDA call needs it.")
        return notes

    @property
    def dimms(self) -> list:
        """Per-module memory detail, from the verbatim inventory.

        The denormalized columns carry the summary a filter needs; the modules
        themselves are only ever read one run at a time, so they stay in the
        JSON rather than becoming a table. Sizes are converted here because a
        template cannot divide.
        """
        memory = ((self.inventory or {}).get("summary") or {}).get("memory") or {}
        modules = []
        for module in memory.get("dimms") or []:
            if not isinstance(module, dict):
                continue
            size = module.get("size_bytes")
            entry = dict(module)
            try:
                entry["size_gb"] = round(int(size) / 1024 ** 3) if size else None
            except (TypeError, ValueError):
                entry["size_gb"] = None
            modules.append(entry)
        return modules

    @property
    def reported_identity_pairs(self) -> list:
        """The (vendor, model) pairs this run reports that could name the machine.

        System table first, board second. A machine whose system table names
        nothing is identified by its board, which is precisely the case an alias
        exists for.
        """
        pairs = []
        if (self.system_product or "").strip():
            pairs.append((self.system_vendor or "", self.system_product))
        if (self.board_model or "").strip():
            pairs.append((self.board_vendor or "", self.board_model))
        return pairs

    @property
    def alias_system_kind(self) -> str:
        """A machine kind a human already corrected for this hardware.

        Cached per instance, and pre-filled in bulk by
        ``services.apply_alias_kinds`` for list pages so a feed of runs is not
        one query per row.
        """
        cached = getattr(self, "_alias_kind", None)
        if cached is None:
            cached = ReportedIdentityAlias.kind_for(self.reported_identity_pairs)
            self._alias_kind = cached
        return cached

    @property
    def effective_system_kind(self) -> str:
        """The machine kind to act on: the submitter's answer, else detection.

        ``system_kind`` stays the raw detected value because that is evidence -
        what the firmware said. This is the *intent*, and it is what every
        display and every listing decision should use. Reading the raw value
        instead left a submitter correcting the kind to "prebuilt" and the page
        still labelling the run "Custom build", which reads as the correction
        not having stuck.
        """
        declared = (self.listing_proposal or {}).get("machine_kind")
        if declared in (SystemKind.PREBUILT, SystemKind.CUSTOM):
            return declared
        # A correction recorded against this hardware applies to every later run
        # of it, which is the entire point of keeping the alias. Without this
        # step a reviewer fixed an HP ProLiant from "custom" to "prebuilt", the
        # alias was written, and the next run of the same server still read
        # "Custom build: HP ProLiant DL360 Gen9" on the landing page.
        if self.alias_system_kind:
            return self.alias_system_kind
        return self.system_kind

    @property
    def effective_vendor(self) -> str:
        """Vendor to show: the submitter's, else whichever table applies."""
        said = ((self.listing_proposal or {}).get("vendor_name") or "").strip()
        if said:
            return said
        if self.effective_system_kind == SystemKind.CUSTOM:
            return self.board_vendor
        return self.system_vendor

    @property
    def effective_product(self) -> str:
        """Model to show, chosen the same way as effective_vendor."""
        said = ((self.listing_proposal or {}).get("name") or "").strip()
        if said:
            return said
        if self.effective_system_kind == SystemKind.CUSTOM:
            return self.board_model
        return self.system_product

    # The field each claimable kind is named by, for a scoped run. Only the kinds a run can
    # actually be scoped to appear: the inventory records one primary part per kind, and a kind
    # with no field here has nothing to name a run after.
    _SCOPE_SUBJECT_FIELDS = {"gpu": "gpu_model", "cpu": "cpu_model", "motherboard": "board_model"}

    @property
    def claim_subject(self) -> str:
        """What a scoped run is a claim about, in words, or "" for a whole-machine run.

        Reported by a submitter whose GPU-only run was titled "Dell Inc. OptiPlex 3080" on the
        dashboard, on the run page, and in the review queue: "I'm still being prompted in the GUI in
        several different ways as if it is a whole system run." Naming the run after the machine is
        the misattribution itself, not a cosmetic problem, because a scoped run deliberately has no
        system listing and asserts nothing whatever about the host.

        The marketing name rather than the raw string, so this reads as the product somebody wants
        certified: lspci names the die and brackets the product, and a page headed "CometLake-S GT2
        [UHD Graphics 630]" names a thing nobody sells.
        """
        from lumina.results.component_match import NORMALIZERS

        names = []
        for kind in self.claim_scope:
            field = self._SCOPE_SUBJECT_FIELDS.get(kind)
            raw = (getattr(self, field, "") or "").strip() if field else ""
            if not raw:
                continue
            normalizer = NORMALIZERS.get(kind)
            names.append((normalizer(raw) if normalizer else raw) or raw)
        return ", ".join(names)

    @property
    def display_name(self) -> str:
        """Human name for what this run is about.

        The submitter's corrections win over what the firmware reported. They
        filled the form in *because* the detection was wrong, so continuing to
        show the detected identity afterwards leaves every list calling a
        Lenovo server "Custom build: OEM 7D2XCTO1WW" while the review page
        shows the corrected name - the same run under two different names
        depending on which page you are on.

        Custom builds are named by their motherboard, prefixed so nobody
        mistakes a board identity mirrored into DMI (for example
        "ASRock B650M PG Riptide") for a vendor system model.

        A scoped run is named by the component it claims. Falling back to the machine when the
        claimed kind was never detected is deliberate: a run has to be identifiable in a list even
        when its subject is unnamed, and ``missing_submission_details`` is what asks for the name.
        """
        if self.is_scoped and self.claim_subject:
            return self.claim_subject
        parts = [
            part for part in (self.effective_vendor, self.effective_product)
            if part
        ]
        if self.effective_system_kind == SystemKind.CUSTOM:
            base = " ".join(parts) or self.cpu_model or str(self.uuid)[:8]
            return f"Custom build: {base}"
        return (
            " ".join(parts)
            or " ".join(p for p in (self.board_vendor, self.board_model) if p)
            or self.cpu_model
            or str(self.uuid)[:8]
        )

    @property
    def host_name(self) -> str:
        """The machine this ran in, named as context rather than as the subject.

        Always the host, even on a scoped run whose ``display_name`` is the card. A page showing a
        component claim still has to say where it was measured, because a driver bound in one
        chassis says little about another.
        """
        parts = [part for part in (self.system_vendor, self.system_product) if part]
        return (
            " ".join(parts)
            or " ".join(p for p in (self.board_vendor, self.board_model) if p)
            or self.cpu_model
            or str(self.uuid)[:8]
        )

    def verdict(self) -> bool | None:
        """Certification verdict for validate runs; None for other run types.

        Mirrors the suite's rule: no required/conditional failures among
        tests that actually ran. Informational results never gate.
        """
        if self.run_type != RunType.validate.value:
            return None
        prefetched = getattr(self, "_prefetched_objects_cache", {})
        if "results" in prefetched:
            # A list page renders this badge on every row, and the EXISTS below
            # is a query each. When the caller has prefetched results, use them:
            # the landing feed was spending one query per validation run purely
            # to decide between PASS and FAIL.
            return not any(
                result.severity != Severity.INFORMATIONAL
                and result.status in (ResultStatus.FAIL, ResultStatus.ERROR)
                for result in prefetched["results"]
            )
        blocking = self.results.exclude(severity=Severity.INFORMATIONAL).filter(
            status__in=[ResultStatus.FAIL, ResultStatus.ERROR]
        )
        return not blocking.exists()

    def status_counts(self) -> dict[str, int]:
        rows = self.results.values("status").annotate(n=models.Count("id"))
        return {row["status"]: row["n"] for row in rows}


class TestResult(models.Model):
    run = models.ForeignKey(TestRun, on_delete=models.CASCADE, related_name="results")
    test_id = models.CharField(max_length=120, db_index=True)
    category = models.CharField(max_length=80, db_index=True)
    severity = models.CharField(max_length=16, choices=Severity.choices, blank=True)
    status = models.CharField(max_length=8, choices=ResultStatus.choices, db_index=True)
    reason = models.TextField(blank=True)
    duration_ms = models.PositiveIntegerField(null=True, blank=True)
    details = models.JSONField(default=dict)

    class Meta:
        ordering = ["test_id"]
        constraints = [
            models.UniqueConstraint(fields=["run", "test_id"], name="testresult_unique_per_run"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.test_id}: {self.status}"


class BenchmarkResult(models.Model):
    """One numeric metric from one benchmark in one run.

    Leaderboards compare within a ``(benchmark_id, benchmark_version, metric)``
    tuple - mixing benchmark definitions would rank incomparable numbers.
    """

    run = models.ForeignKey(TestRun, on_delete=models.CASCADE, related_name="benchmarks")
    benchmark_id = models.CharField(max_length=120, db_index=True)
    benchmark_version = models.CharField(max_length=40, default="1")
    category = models.CharField(max_length=80, db_index=True)
    metric = models.CharField(max_length=80)
    value = models.DecimalField(max_digits=24, decimal_places=6)
    unit = models.CharField(max_length=40)
    direction = models.CharField(
        max_length=20, choices=MetricDirection.choices, default=MetricDirection.INFO
    )
    is_primary = models.BooleanField(default=False)
    context = models.JSONField(default=dict)
    # Per-device GPU results. device_raw is ground truth (the verbatim clpeak device string);
    # device_model is lumina's canonicalization of it (via normalize_gpu_model) and is the
    # leaderboard/compare grouping key. Both are blank on non-GPU metrics and on pre-device-field
    # reports, so those rows keep exactly one entry per (run, benchmark_id, metric). device_ordinal
    # is the 0-based position within an identically named GPU group, above 0 only under --all-gpus
    # so two identical cards become distinct rows; non-null (default 0) so the unique constraint
    # below holds on MariaDB, where NULL is treated as distinct and would defeat the guard.
    device_raw = models.CharField(max_length=200, blank=True, db_index=True)
    device_model = models.CharField(max_length=200, blank=True, db_index=True)
    device_ordinal = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["benchmark_id", "metric"]
        constraints = [
            models.UniqueConstraint(
                fields=["run", "benchmark_id", "metric", "device_raw", "device_ordinal"],
                name="benchmarkresult_unique_per_run",
            ),
        ]
        indexes = [
            models.Index(
                fields=["benchmark_id", "benchmark_version", "metric"],
                name="benchmark_leaderboard_idx",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.benchmark_id}.{self.metric}={self.value}{self.unit}"

    @property
    def display_label(self) -> str:
        """The benchmark's human name. ``bench.cpu.sysbench-multi`` is an
        identifier, and showing it to the public is showing our notes."""
        from lumina.results.highlights import benchmark_label

        return benchmark_label(self.benchmark_id)

    @property
    def display_value(self) -> str:
        """The measurement with grouped digits and precision that matches it."""
        from lumina.results.highlights import format_metric

        return format_metric(self.value)

    @property
    def metric_display(self) -> str:
        """The metric named as a reader would say it, rather than as it is keyed.

        ``vulkan_global_memory_bandwidth`` is what makes two runs comparable and what the
        leaderboard is pinned to, and it is not what belongs in a table cell.
        """
        from lumina.results.highlights import metric_label

        return metric_label(self.metric)

    @property
    def gpu_api_label(self) -> str:
        """Which GPU API produced this figure, or "" when the metric names none.

        Its own property because it is a badge rather than part of the name: a CUDA number and an
        OpenCL number for one card measure different software, so a table of GPU results that does
        not say which is which is not readable.
        """
        from lumina.results.gpu_metrics import describe

        return describe(self.metric)["api_label"]

    @property
    def gpu_group_label(self) -> str:
        """clpeak's own category for this test - compute, bandwidth, or latency."""
        from lumina.results.gpu_metrics import describe, is_gpu_metric

        return describe(self.metric)["group_label"] if is_gpu_metric(self.metric) else ""

    @property
    def gpu_tag_label(self) -> str:
        """The test alone, with no API in front of it.

        For a table that already carries the API as a badge, where ``metric_display`` would repeat
        it: "Vulkan Compute - Vulkan double precision". ``metric_display`` keeps the API for the
        places that show the name on its own, such as a comparison row.
        """
        from lumina.results.gpu_metrics import describe, is_gpu_metric

        return describe(self.metric)["tag_label"] if is_gpu_metric(self.metric) else ""


class RunArtifact(models.Model):
    run = models.ForeignKey(TestRun, on_delete=models.CASCADE, related_name="artifacts")
    file = models.FileField(upload_to=_artifact_upload_to, max_length=400)
    bundle_path = models.CharField(max_length=300)
    sha256 = models.CharField(max_length=64)
    size = models.PositiveBigIntegerField(default=0)

    class Meta:
        ordering = ["bundle_path"]
        constraints = [
            models.UniqueConstraint(
                fields=["run", "bundle_path"], name="runartifact_unique_per_run"
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.bundle_path


class ReportedIdentityAlias(models.Model):
    """A reported hardware identity that a human has already resolved.

    Firmware is not always a usable identity. A Lenovo server reports vendor
    "OEM" and product "7D2XCTO1WW"; somebody works out that it is a ThinkSystem
    SR645 and names the listing accordingly. The next run of that same machine
    reports the same unhelpful strings, matches nothing, and asks the next
    submitter to work it out again - who may name it differently and fork the
    catalog.

    This is that resolution kept as data: "when a run reports *this*, it means
    *that listing*". ``VendorAlias`` already does the same job for the
    manufacturer string alone; this covers the model, which had no equivalent,
    and points at a System or a Component (a custom build is identified by its
    motherboard, so board strings need the same treatment).

    Matching is case-insensitive on both halves, and the vendor may be blank
    because firmware that reports no manufacturer is exactly the case this
    exists for.
    """

    reported_vendor = models.CharField(
        max_length=200, blank=True,
        help_text="Manufacturer string as the firmware reports it. May be "
                  "blank: unbranded firmware reports none, which is precisely "
                  "when this mapping is needed.",
    )
    reported_product = models.CharField(
        max_length=200,
        help_text="Model string as the firmware reports it, e.g. Lenovo's "
                  "machine-type code “7D2XCTO1WW”.",
    )
    # The kind correction is part of the mapping, not a per-run detail.
    # Prebuilt systems frequently fail to identify themselves - a vendor that
    # mirrors its system name into the baseboard reads as a custom build - and
    # without this every future run of that machine is misclassified again and
    # the submitter has to correct it again.
    #
    resolved_kind = models.CharField(
        max_length=10, choices=SystemKind.choices, blank=True,
        help_text="What this machine actually is, overriding what the firmware "
                  "led the collector to guess.",
    )
    listing_system = models.ForeignKey(
        "hardware.System", null=True, blank=True, on_delete=models.CASCADE,
        related_name="reported_aliases",
    )
    listing_component = models.ForeignKey(
        "hardware.Component", null=True, blank=True, on_delete=models.CASCADE,
        related_name="reported_aliases",
    )
    # Provenance, so a wrong mapping can be traced to the decision that made it
    # rather than just deleted and forgotten.
    source_run = models.ForeignKey(
        "results.TestRun", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="identity_aliases",
        help_text="The run whose review established this mapping, when it came "
                  "from one rather than being entered by hand.",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="+",
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name_plural = "reported identity aliases"
        ordering = ["reported_vendor", "reported_product"]
        constraints = [
            models.CheckConstraint(
                name="reported_alias_exactly_one_listing",
                condition=(
                    models.Q(listing_system__isnull=False,
                             listing_component__isnull=True)
                    | models.Q(listing_system__isnull=True,
                               listing_component__isnull=False)
                ),
            ),
            # One reported identity cannot mean two things, or auto-linking
            # would depend on row order.
            models.UniqueConstraint(
                fields=["reported_vendor", "reported_product"],
                name="unique_reported_identity",
            ),
        ]

    def __str__(self) -> str:
        reported = " ".join(
            part for part in (self.reported_vendor, self.reported_product) if part
        )
        return f"{reported} -> {self.listing}"

    @property
    def listing(self):
        return self.listing_system or self.listing_component

    @classmethod
    def for_identity(cls, reported_vendor: str, reported_product: str):
        """The alias row for a reported identity, or None."""
        product = (reported_product or "").strip()
        if not product:
            return None
        return (
            cls.objects.filter(
                reported_vendor__iexact=(reported_vendor or "").strip(),
                reported_product__iexact=product,
            )
            .select_related("listing_system", "listing_component")
            .first()
        )

    @classmethod
    def kind_for(cls, pairs) -> str:
        """The corrected machine kind for the first identity a human ruled on."""
        for vendor, product in pairs:
            alias = cls.for_identity(vendor, product)
            if alias and alias.resolved_kind:
                return alias.resolved_kind
        return ""

    @classmethod
    def resolve(cls, reported_vendor: str, reported_product: str):
        """The listing a reported identity maps to, or None.

        Case-insensitive because firmware capitalization is not stable across
        BIOS revisions of the same machine.
        """
        alias = cls.for_identity(reported_vendor, reported_product)
        return alias.listing if alias else None
