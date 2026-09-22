"""Hardware listings, submissions, and community attestations.

Design notes
------------
Systems and Components share a lot of fields but are kept as separate
concrete models (rather than using multi-table inheritance or a generic FK)
because the public URLs, admin listings, and filter facets differ between
them. The shared fields live on an abstract ``HardwareListing`` base.

``Submission`` points at a listing via two nullable FKs (``listing_system``,
``listing_component``). Exactly one is set. A generic FK would be more
elegant but generic relations are painful with Django admin, Jazzmin, and
DRF serializers - two FKs are simpler and the constraint is easy to enforce.

Re-validation: a Submission whose listing is already published counts as a
re-validation. Approving it increments ``attestation_count`` and may upgrade
the listing's ``validation_level`` (never downgrade).
"""
from __future__ import annotations

import json
import re
import uuid
from enum import StrEnum
from pathlib import Path
from typing import override

from django.conf import settings
from django.db import models
from django.utils import timezone

from lumina.core.certification import (
    LEVEL_RANK,
    ValidationLevel,
    highest_level,
    level_outranks,
)
from lumina.core.models import VendorSlugMixin
from lumina.core.review import ReviewWorkflow


class ComponentKind(StrEnum):
    """Structural kind of a Component listing.

    Used to drive UI choices (the submit form's CPU family picker lists only
    Components with ``kind=cpu``) without forcing every reference through a
    separate table per kind.
    """

    cpu = "cpu"
    motherboard = "motherboard"
    gpu = "gpu"
    nic = "nic"
    storage = "storage"
    management = "management"
    other = "other"

    @property
    def label(self) -> str:
        """Human label. Acronyms stay uppercase, so never .capitalize() a
        kind value directly - use this (or get_kind_display) everywhere."""
        return _COMPONENT_KIND_LABELS.get(self.value, self.value.capitalize())

    @classmethod
    def choices(cls) -> list[tuple[str, str]]:
        return [(k.value, k.label) for k in cls]


# Kinds whose display form is not just a capitalized value.
_COMPONENT_KIND_LABELS = {
    "cpu": "CPU",
    "gpu": "GPU",
    "nic": "NIC",
}


class ComponentRole(models.TextChoices):
    """Whether a component entry names one part or a generation of them.

    Both live in the same table because both are browsable, certifiable
    listings, but they play different roles and the distinction must be
    declared rather than inferred: a MODEL is a specific part as reported by
    the hardware ("AMD EPYC 9354"); a FAMILY covers a generation ("AMD EPYC
    9004 Series") and owns the patterns that decide which reported models
    roll up to it. Certification is granted at family level; benchmarks stay
    per model.
    """

    MODEL = "model", "Specific model"
    FAMILY = "family", "Family / generation"


class HardwareListing(VendorSlugMixin, models.Model):
    """Abstract base for System and Component.

    Concrete subclasses pick up everything here plus whatever they define
    (e.g. System-specific M2M to components).
    """

    name = models.CharField(max_length=200)
    vendor = models.ForeignKey(
        "vendors.Vendor", on_delete=models.PROTECT, related_name="+",
        help_text="The hardware's manufacturer.",
    )
    # Distinct related_name per concrete model - abstract bases would
    # otherwise produce two clashing ``Vendor.owned_listings`` accessors.
    # Result: ``vendor.owned_systems.all()`` and ``vendor.owned_components.all()``.
    owner_vendor = models.ForeignKey(
        "vendors.Vendor", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="owned_%(class)ss",
        help_text=(
            "Vendor that maintains this listing. Submit-role members of this "
            "vendor may submit edit proposals via the self-service flow. "
            "Null means no vendor maintains it (community-submitted); only "
            "admins can edit those listings."
        ),
    )
    model_number = models.CharField(max_length=120, blank=True)
    description = models.TextField(blank=True)
    vendor_spec_url = models.URLField(
        blank=True,
        help_text=(
            "Link to the vendor's spec sheet for this listing. Surfaced as a "
            "CTA on the public detail page."
        ),
    )
    slug = models.SlugField(max_length=220, unique=True, blank=True)

    published = models.BooleanField(default=False)
    validation_level = models.CharField(
        max_length=16,
        choices=ValidationLevel.choices,
        default=ValidationLevel.COMMUNITY,
    )
    attestation_count = models.PositiveIntegerField(default=0)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    # When this listing was last certified by anybody, derived from its attestations by
    # ``hardware.services.recompute_listing_levels`` - never set by hand, like the two columns
    # above it there.
    #
    # Denormalized for the same reason ``attestation_count`` is: the browse page sorts on it,
    # and a Max() over a join on the busiest page in the catalog, under filters that already
    # join, is a cost paid on every render and every count query. Indexed, and null for a
    # listing nothing has certified yet - a vendor's declared support is not a certification.
    last_certified_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        abstract = True
        ordering = ["name"]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.vendor} {self.name}"

    def get_absolute_url(self) -> str:
        """The listing's public page. One detail view serves both subclasses by slug.

        Here rather than in each template that links a listing: a sitemap needs it, and the naming
        plan needed it too - it was rendering ``href=""`` for want of this.
        """
        from django.urls import reverse

        return reverse("hardware:detail", args=[self.slug])


class System(HardwareListing):
    related_components = models.ManyToManyField(
        "hardware.Component",
        blank=True,
        related_name="systems",
        help_text="Loose cross-reference to certified components inside this system.",
    )
    cpus = models.ManyToManyField(
        "hardware.Component",
        blank=True,
        related_name="cpu_of_systems",
        limit_choices_to={"kind": ComponentKind.cpu.value},
        help_text="CPU-family components certified in this system. Added "
                  "automatically by approved, passing validation runs; one "
                  "entry per family a run has actually proven.",
    )
    supported_cpus = models.ManyToManyField(
        "hardware.Component",
        blank=True,
        related_name="supported_in_systems",
        limit_choices_to={"kind": ComponentKind.cpu.value},
        help_text="CPU families the vendor states this system accepts, whether "
                  "or not one has been validated here. A Supermicro "
                  "SYS-1029U-TN10RT takes both 1st and 2nd generation Xeon "
                  "Scalable; validating it with one generation says nothing "
                  "about the other, so the two are recorded separately.",
    )

    class Meta(HardwareListing.Meta):
        abstract = False

    def cpu_support(self) -> list:
        """Every CPU family this system relates to, with its provenance.

        Returns ``[{"cpu": Component, "validated": bool}]``, validated first.
        Keeping declared support and validation evidence in one list is what
        lets the page show "supports three generations, two of them proven"
        instead of quietly presenting a spec-sheet claim as a test result.
        """
        validated = list(self.cpus.select_related("vendor").all())
        validated_ids = {cpu.pk for cpu in validated}
        declared = [
            cpu for cpu in self.supported_cpus.select_related("vendor").all()
            if cpu.pk not in validated_ids
        ]
        return (
            [{"cpu": cpu, "validated": True} for cpu in validated]
            + [{"cpu": cpu, "validated": False} for cpu in declared]
        )

    def socket_counts(self) -> list[int]:
        """Physical-socket configurations this system has been validated in, ascending.

        Derived from the socket count each public run cited, never stored: socket count is not a
        validation discriminator - a run in any configuration validates the whole system - so this
        is documentation and a benchmark-context hint, not a certification attribute. A dual-capable
        chassis run once with one CPU and once with two lists both. ``public()`` scopes it to
        approved, published, non-embargoed runs, so it shows the same evidence the rest of the page
        does.
        """
        from lumina.results.models import TestRun

        # ``order_by`` before ``distinct`` on purpose: TestRun has a default Meta ordering, and a
        # bare ``.values_list(col).distinct()`` would carry that ordering column into the SELECT, so
        # DISTINCT would run over two columns and let repeats through. Ordering by the one column we
        # select keeps DISTINCT single-column and returns the list already sorted.
        return list(
            TestRun.objects.public()
            .filter(listing_system=self, cpu_sockets__isnull=False)
            .order_by("cpu_sockets")
            .values_list("cpu_sockets", flat=True)
            .distinct()
        )


class Component(HardwareListing):
    kind = models.CharField(
        max_length=16,
        choices=ComponentKind.choices(),
        default=ComponentKind.other.value,
        db_index=True,
    )
    # Kind-specific details without a table per kind: components must fit
    # "basically any PCIe device" while still carrying the specifics that
    # matter for the common ones. Conventional keys - storage:
    # {"media": "hdd|ssd", "interface": "nvme|sata|sas"}; gpu:
    # {"vram_gb": 24, "driver": "nvidia 570.86"}; nic: {"speed_mbps": 25000}.
    attributes = models.JSONField(default=dict, blank=True)
    # Which naming rule produced this component's name, when one did. Recorded because "why is it
    # called that" is the question this whole table exists to answer, and a name with no
    # provenance sends a reviewer reading regexes.
    named_by = models.ForeignKey(
        "hardware.ComponentNamingRule", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="named_components",
    )
    role = models.CharField(
        max_length=8,
        choices=ComponentRole.choices,
        default=ComponentRole.MODEL,
        db_index=True,
        help_text=(
            "A specific model is one part as the hardware reports it. A "
            "family covers a generation and owns the matching patterns below."
        ),
    )
    # Regexes matching the raw model strings that belong to this family, e.g.
    # ["EPYC 9[0-9]{2}4\\b"] on "AMD EPYC 9004 Series". A component with
    # patterns *is* a family: certification is granted at family granularity
    # (one entry covers a generation), while benchmarks stay per-model, so
    # families are resolved from stored model strings at display time rather
    # than baked in at ingest. Curated in the admin - model->generation is
    # domain knowledge no string comparison can derive.
    model_patterns = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            "One regular expression per line, matched case-insensitively "
            "against reported model strings. Leave empty for a specific "
            "model rather than a family. Keep a family to a single generation "
            "and socket: certifying one member certifies every model the "
            "patterns match, so a pattern spanning sockets (e.g. all “Xeon E” "
            "generations, which change socket across E-2100/2200/2300/2400) "
            "would certify hardware the run never ran on. Prefer per-"
            "generation families like “Xeon E-2300 Series”."
        ),
    )

    class Meta(HardwareListing.Meta):
        abstract = False

    @property
    def is_family(self) -> bool:
        return self.role == ComponentRole.FAMILY

    @property
    def display_label(self) -> str:
        """Vendor and name, without repeating a vendor the name already carries.

        The curated families are named the way the vendor writes the product -
        "AMD Ryzen 7000 Series", "NVIDIA GeForce RTX 40 Series" - so prefixing
        the vendor again reads "AMD AMD Ryzen 7000 Series". Model-level entries
        like "0M83RH" carry no vendor and do need it.

        ``__str__`` is left alone: it is what the admin and the audit log show,
        and changing it would move that text everywhere at once.
        """
        vendor = self.vendor.name if self.vendor_id else ""
        if not vendor:
            return self.name
        if self.name.lower().startswith(vendor.lower()):
            return self.name
        return f"{vendor} {self.name}"

    def used_in_systems(self) -> list:
        """Published systems this component appears in, and how.

        Returns ``[{"system": System, "relation": str}]`` where relation is
        ``validated`` (a passing run proved this CPU family in that system),
        ``supported`` (the vendor states the system accepts it), or ``present``
        (the part was found inside that system, which is how motherboards and
        GPUs relate).

        Rolls up through the family/model pair in **both** directions, because
        certification attaches families while benchmarks record models. Someone
        landing on "Xeon Gold 6430" wants the systems certified for its
        generation, and someone on the generation page wants systems recorded
        against any of its members. Without that, a model page would sit there
        empty while its family listed a dozen machines.

        Only published systems, so an embargoed machine is not revealed here.
        """
        related = [self.pk]
        if self.is_family:
            related.extend(self.matching_models().values_list("pk", flat=True))
        else:
            family = self.resolved_family()
            if family is not None:
                related.append(family.pk)

        published = System.objects.filter(published=True).select_related("vendor")
        # Ordered strongest-claim-first, and deduplicated in that order: a
        # system that both declares support and has validated it should read as
        # validated, not as a claim.
        buckets = (
            ("validated", published.filter(cpus__in=related)),
            ("supported", published.filter(supported_cpus__in=related)),
            ("present", published.filter(related_components__in=related)),
        )
        seen, out = set(), []
        for relation, queryset in buckets:
            for system in queryset.order_by("vendor__name", "name").distinct():
                if system.pk in seen:
                    continue
                seen.add(system.pk)
                out.append({"system": system, "relation": relation})
        return out

    def matching_models(self) -> models.QuerySet[Component]:
        """Model-role components whose names this family's patterns match.

        Resolved on read, so the association shown in the UI is always the
        one the patterns actually produce - nothing cached to drift.
        """
        if not self.is_family:
            return Component.objects.none()
        from lumina.results.component_match import matches_family

        candidates = Component.objects.filter(
            kind=self.kind, role=ComponentRole.MODEL, vendor=self.vendor
        )
        ids = [c.pk for c in candidates if matches_family(self, c.name)]
        return Component.objects.filter(pk__in=ids)

    def resolved_family(self):
        """The family this model rolls up to, or None."""
        if self.is_family:
            return None
        from lumina.results.component_match import family_for_model

        return family_for_model(self.name, ComponentKind(self.kind),
                                vendor=self.vendor)

    @classmethod
    def of_kind(cls, kind: ComponentKind | str) -> models.QuerySet[Component]:
        return cls.objects.filter(kind=ComponentKind(kind).value)


def _test_result_upload_to(instance: TestResultAttachment, filename: str) -> str:
    # Namespace uploads under the submission UUID so a single directory
    # contains all files for one review context.
    return str(Path("test-results") / str(instance.submission.uuid) / filename)


class Submission(ReviewWorkflow, models.Model):
    review_noun = "submission"

    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    submitter = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="submissions",
    )
    on_behalf_of = models.ForeignKey(
        "vendors.Vendor",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="submissions",
    )
    listing_system = models.ForeignKey(
        System,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="submissions",
    )
    listing_component = models.ForeignKey(
        Component,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="submissions",
    )
    claimed_validation_level = models.CharField(
        max_length=16, choices=ValidationLevel.choices, default=ValidationLevel.COMMUNITY
    )
    # Which AlmaLinux majors this submission actually claims, recorded at submit time.
    #
    # ``approve`` used to re-derive this by walking ``listing.versions.all()``, on the
    # reasoning that the submit form had just written those rows so the listing was
    # where they lived. That held only for a listing the form created. Once any
    # submission could name an existing listing, approval attested every release the
    # listing had ever carried: a submission citing *no release at all* minted two
    # attestations against a listing that already had 8 and 10, and the public page
    # counted them as "community members who independently confirmed it by running the
    # suite". The re-validation flow is gone, so the walk would be correct again today
    # - but correct by coincidence, and it would break the moment anything else can add
    # a version to a draft listing (a reviewer tweak, an edit proposal). The claim is a
    # property of the submission, so it is stored on the submission.
    cited_releases = models.ManyToManyField(
        "releases.AlmaLinuxRelease", blank=True, related_name="hardware_submissions",
    )

    status = models.CharField(
        max_length=16,
        choices=ReviewWorkflow.STATUS_CHOICES,
        default=ReviewWorkflow.STATUS_PENDING,
    )
    reviewer_notes = models.TextField(blank=True)
    submitter_notes = models.TextField(blank=True)

    submitted_at = models.DateTimeField(auto_now_add=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_submissions",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-submitted_at"]
        constraints = [
            models.CheckConstraint(
                name="submission_exactly_one_listing",
                condition=(
                    models.Q(listing_system__isnull=False, listing_component__isnull=True)
                    | models.Q(listing_system__isnull=True, listing_component__isnull=False)
                ),
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"Submission {self.uuid} ({self.status})"

    # --- listing accessor ---------------------------------------------------
    @property
    def listing(self) -> HardwareListing:
        return self.listing_system or self.listing_component  # type: ignore[return-value]

    # --- state transitions --------------------------------------------------
    # ``_require_pending`` used to live here. It was misnamed: its body accepted
    # PENDING *or* NEEDS_CHANGES, which is exactly ``ReviewWorkflow._require_open``.
    # The name read as a stricter rule than the code implemented, which is why this
    # looked like the one model that could not share the guard.

    # The most a manual submission can ever be worth, whoever files it and whoever
    # approves it. A submission is a *declaration*: somebody says this hardware works.
    # The vendor and AlmaLinux badges are claims about verified evidence, and the only
    # thing that produces verified evidence is a manifest-checked passing run from the
    # certification suite. Nothing on this path opens, parses, or hashes an attachment
    # against anything, so nothing on this path can earn them.
    MANUAL_CEILING = ValidationLevel.COMMUNITY

    def approve(self, *, by, final_level: str) -> None:
        # Clamped here rather than in the view, because the view is one door. The
        # reviewer's dropdown no longer offers the upper tiers, and ``review:approve``
        # re-derives through ``resolve_claimed_level`` as well, but a direct
        # ``submission.approve(by=x, final_level=VENDOR)`` used to be enough on its own
        # to hang a Vendor-validated badge on a listing with no runs behind it - and
        # ``review:approve`` accepted the tier straight off an unauthenticated-in-any-
        # meaningful-sense POST body, falling back to the submitter's own claim when
        # the field was absent entirely.
        #
        # Clamp rather than raise: the only way to ask for more is now a hand-crafted
        # request, which deserves the correct outcome and not a 500. A reviewer acting
        # through the UI cannot reach this.
        if level_outranks(final_level, self.MANUAL_CEILING):
            final_level = self.MANUAL_CEILING

        self._require_open("approve")
        self._record_approval(by=by)

        listing = self.listing
        listing.published = True
        listing.save(update_fields=["published"])

        # Cascade-publish anything inline-proposed alongside this submission:
        # the listing's vendor (if it was created inline as a draft) and any
        # CPU components attached to the listing that are still draft. Reviewer
        # already saw and approved them as part of this submission.
        if not listing.vendor.published:
            listing.vendor.published = True
            listing.vendor.save(update_fields=["published"])
        if isinstance(listing, System):
            for cpu in listing.cpus.filter(published=False):
                cpu.published = True
                cpu.save(update_fields=["published"])

        # One attestation per release this submission cites, mirroring
        # SoftwareSubmission.approve. Read off ``cited_releases`` - see that field for
        # why walking the listing's versions instead was wrong.
        #
        # A submission citing no release attests nothing rather than attaching
        # evidence to an invented one; the listing still publishes, and
        # recompute_listing_levels floors its tier.
        #
        # Local import: hardware.services imports this module.
        from lumina.hardware.services import recompute_listing_levels, record_attestation

        for version in listing.versions.filter(
            release__in=self.cited_releases.all()
        ):
            record_attestation(
                version=version, listing=listing, attested_by=self.submitter,
                level=final_level, source=self,
            )

        recompute_listing_levels(listing)

    # reject / request_changes come from ReviewWorkflow.


class TestResultAttachment(models.Model):
    submission = models.ForeignKey(
        Submission, on_delete=models.CASCADE, related_name="attachments"
    )
    file = models.FileField(upload_to=_test_result_upload_to)
    description = models.CharField(max_length=300, blank=True)
    sha256 = models.CharField(max_length=64, blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.description or self.file.name


class ListingCategoryValue(models.Model):
    """Binds a listing (System XOR Component) to a taxonomy CategoryValue.

    Splitting this out of HardwareListing keeps the M2M join explicit and
    lets us hang extra per-binding metadata later (e.g. notes for why this
    listing was tagged with a given value). Exactly one of ``listing_system``
    / ``listing_component`` is set - enforced by a CheckConstraint.
    """

    listing_system = models.ForeignKey(
        System, on_delete=models.CASCADE, null=True, blank=True,
        related_name="category_values",
    )
    listing_component = models.ForeignKey(
        Component, on_delete=models.CASCADE, null=True, blank=True,
        related_name="category_values",
    )
    value = models.ForeignKey(
        "taxonomy.CategoryValue", on_delete=models.PROTECT, related_name="listing_bindings"
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                name="listing_value_exactly_one_listing",
                condition=(
                    models.Q(listing_system__isnull=False, listing_component__isnull=True)
                    | models.Q(listing_system__isnull=True, listing_component__isnull=False)
                ),
            ),
            models.UniqueConstraint(
                fields=["listing_system", "value"],
                name="unique_system_value",
            ),
            models.UniqueConstraint(
                fields=["listing_component", "value"],
                name="unique_component_value",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.listing_system or self.listing_component}: {self.value}"


class ListingVersion(models.Model):
    """Binds a listing to one AlmaLinux **major** release. The unit of validation.

    Example: ``(PowerEdge R750, AlmaLinux 9)`` means the system is certified on AlmaLinux 9.

    It used to carry a ``minimum_minor`` floor, so the same row could say "9.4 and later".
    That was evidence-honest - a 9.6 pass does not prove 9.0 - and it is gone deliberately:
    it made hardware's unit of certification differ from software's, no consumer used it (the
    catalog filter asks "does this run on 9"), and a promise about which minors work is one
    the project cannot keep across a major's whole life.

    The minor a run passed on is still recorded, on ``TestRun.alma_minor``. It is provenance
    for a piece of evidence rather than the scope of a claim.

    Split out of ``ListingCategoryValue`` because AlmaLinux releases are structured and
    ordered rather than an admin-curated free-text taxonomy.
    """

    listing_system = models.ForeignKey(
        System, on_delete=models.CASCADE, null=True, blank=True,
        related_name="versions",
    )
    listing_component = models.ForeignKey(
        Component, on_delete=models.CASCADE, null=True, blank=True,
        related_name="versions",
    )
    release = models.ForeignKey(
        "releases.AlmaLinuxRelease", on_delete=models.PROTECT, related_name="+",
    )
    # Where this row came from. record_compatibility writes rows off the back
    # of a passing run, which is evidence; a human adding one is making a claim.
    # A Ryzen 7000 obviously runs on AlmaLinux 9, but until a run proves it the
    # catalog should not present the two as the same kind of statement.
    SOURCE_RUN = "run"
    SOURCE_DECLARED = "declared"
    SOURCE_CHOICES = [
        (SOURCE_RUN, "Proven by a validation run"),
        (SOURCE_DECLARED, "Declared, not yet validated"),
    ]
    source = models.CharField(
        max_length=10, choices=SOURCE_CHOICES, default=SOURCE_DECLARED,
        help_text=(
            "Rows created by an approved validation run are marked as proven. "
            "Anything added by hand is a declaration until a run confirms it."
        ),
    )
    available_from_minor = models.PositiveSmallIntegerField(
        null=True, blank=True,
        help_text=(
            "The minor of this major in which the hardware enablement lands, when the evidence "
            "came from AlmaLinux Kitten. The listing is published and carries a disclaimer "
            "naming this minor until ``AlmaLinuxRelease.latest_minor`` reaches it. Null means "
            "the claim is live."
        ),
    )
    # Derived from this row's attestations by
    # ``hardware.services.recompute_listing_levels`` - never set by hand.
    #
    # Blank rather than floored at community, because a declared row has no
    # evidence at all: a vendor typing "it runs on 9" is not the same statement as
    # a passing run, and hardware keeps that distinction where software has none.
    # A tier here would claim trust nothing earned.
    #
    # Empty string rather than NULL (DJ001): two ways to spell "no tier" is a bug
    # waiting to happen, and both are falsy so every consumer reads the same.
    validation_level = models.CharField(
        max_length=16, choices=ValidationLevel.choices, blank=True, default="",
        db_index=True,
        help_text=(
            "Highest tier among this release's attestations. Empty while the row "
            "is only declared."
        ),
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                name="listing_version_exactly_one_listing",
                condition=(
                    models.Q(listing_system__isnull=False, listing_component__isnull=True)
                    | models.Q(listing_system__isnull=True, listing_component__isnull=False)
                ),
            ),
            models.UniqueConstraint(
                fields=["listing_system", "release"],
                name="unique_system_release",
            ),
            models.UniqueConstraint(
                fields=["listing_component", "release"],
                name="unique_component_release",
            ),
        ]
        # Newest major first. One row per (listing, major) now that the minor floor is gone,
        # so the major is the whole sort key.
        ordering = ["-release__major"]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.display

    @property
    def display(self) -> str:
        """Human-readable label, e.g. 'AlmaLinux 9'.

        Majors only. This carried a ``minimum_minor`` floor and rendered "AlmaLinux 9.4+",
        on the reasoning that a 9.6 pass does not assert 9.0. True, but it made hardware's
        unit of certification different from software's for no benefit anybody used: the
        catalog filter never looked at the floor ("show me things that run on AlmaLinux 9"
        is the question people ask), and a per-minor claim is a promise the project cannot
        keep across a major's whole life anyway.

        The minor a run passed on is still recorded, on ``TestRun.alma_minor``, and shown
        wherever a run is shown - it is provenance for the evidence rather than the scope
        of the claim.
        """
        return f"AlmaLinux {self.release.major}"

    @property
    def awaiting_major_release(self) -> bool:
        """Whether this claim names a major that has not been released at all.

        Its own gate, independent of ``available_from_minor``. A run on AlmaLinux Kitten 11
        genuinely certifies the major, and a submitter who does not know which minor carries
        their patch leaves that box empty - which would otherwise publish a flat "AlmaLinux 11"
        claim while AlmaLinux 11 does not exist.

        True for a declared row on such a major too. A vendor stating support for an unreleased
        major is making a real claim, and "11 is not out yet" is a fact about it either way.
        """
        return not self.release.is_released

    @property
    def pending_minor(self) -> int | None:
        """The minor this claim is still waiting on, or None if it is live.

        A row is gated only while the minor it names has not shipped. Nothing schedules the
        lift: ``latest_minor`` is admin-maintained and this is read at display time, so raising
        that number is the single action that clears the disclaimer from every listing waiting
        on it. A claim that depended on a cron firing would be wrong whenever the cron was not.
        """
        if self.available_from_minor is None:
            return None
        if self.release.minor_is_live(self.available_from_minor):
            return None
        return self.available_from_minor

    @property
    def disclaimer(self) -> str:
        """What to say about a claim whose minor has not shipped, or "" when it is live.

        Wording per the SIG's decision: the major is confirmed, and it was confirmed on Kitten,
        with the enablement landing in a named minor. All three facts, because "not yet
        available" alone would read as a failure rather than as a schedule.
        """
        major = self.release.major
        pending = self.pending_minor
        if pending is None and not self.awaiting_major_release:
            return ""

        # Two facts, and either can hold on its own. A named minor that has not shipped, and a
        # major with no stable release at all - the state a Kitten-tracked major sits in for
        # months before its first release, expected to run six months to a year for 11.
        if self.awaiting_major_release:
            lead = (
                f"AlmaLinux {major} support confirmed using AlmaLinux Kitten. "
                f"AlmaLinux {major} has not been released yet"
            )
            if pending is None:
                return f"{lead}."
            return f"{lead}; the hardware enablement lands in AlmaLinux {major}.{pending}."
        return (
            f"AlmaLinux {major} support confirmed using AlmaLinux Kitten. "
            f"The hardware enablement lands in AlmaLinux {major}.{pending}."
        )

    def official_levels(self) -> list[str]:
        """The tiers a *party* asserted for this release: vendor, AlmaLinux, or both.

        Hardware keeps official assertions and community confirmations in one table,
        told apart only by ``level``, so a page that shows a single derived tier
        credits the vendor with the community's runs and hides the community's
        contribution inside the count beside it. This is the half that answers "who
        validated this".

        Both can be present: a release certified by its vendor *and* by the
        Foundation is two separate statements, and reducing them to one badge throws
        away half the answer. Software has the same shape, with its
        ``certifications`` in their own table.

        Ordered by descending rank so the strongest reads first.
        """
        levels = {
            attestation.level for attestation in self.attestations.all()
            if attestation.level != ValidationLevel.COMMUNITY
        }
        return sorted(levels, key=lambda level: -LEVEL_RANK[ValidationLevel(level)])

    def community_confirmations(self) -> int:
        """How many people confirmed this release, not counting official runs.

        The other half. A vendor engineer's run is an assertion, not a
        confirmation of somebody else's, so counting it here would inflate what
        the community actually did.
        """
        return sum(
            1 for attestation in self.attestations.all()
            if attestation.level == ValidationLevel.COMMUNITY
        )

    def derived_level(self) -> str:
        """The highest tier among this release's attestations, or "" if it has none.

        Mirrors ``SoftwareCompatibility.derived_level`` but returns empty on an
        empty set instead of flooring at community, because on hardware an
        attestation-free row is a *declaration* rather than a community
        confirmation. See the field's own comment.

        Delegates to ``highest_level`` rather than folding with ``level_outranks``
        by hand, so the one ranking rule governs this too.
        """
        levels = [attestation.level for attestation in self.attestations.all()]
        if not levels:
            return ""
        return highest_level(levels)


class ListingEditProposal(ReviewWorkflow, models.Model):
    """Pending edit to a vendor-owned hardware listing.

    Mirrors VendorProposal's update flow: vendor-owned listings can be
    edited via this proposal queue rather than directly. Reviewers approve
    or reject; on approval the proposal's non-blank fields are copied to
    the live listing. Blank values mean "no change" so a sparse form
    submission doesn't accidentally clear other fields.

    Two FKs to System/Component (exactly one set) keep this consistent with
    Submission and ListingCategoryValue. Generic relations were considered
    and rejected - they fight Django admin and DRF serializers more than
    they're worth here.
    """

    review_noun = "listing edit"

    listing_system = models.ForeignKey(
        System, on_delete=models.CASCADE, null=True, blank=True,
        related_name="edit_proposals",
    )
    listing_component = models.ForeignKey(
        Component, on_delete=models.CASCADE, null=True, blank=True,
        related_name="edit_proposals",
    )
    proposed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="listing_edit_proposals",
    )

    # Proposed field values; names mirror HardwareListing for the loop in
    # ``approve``.
    name = models.CharField(max_length=200, blank=True)
    model_number = models.CharField(max_length=120, blank=True)
    description = models.TextField(blank=True)
    vendor_spec_url = models.URLField(blank=True)

    submitter_notes = models.TextField(blank=True)
    status = models.CharField(
        max_length=16,
        choices=ReviewWorkflow.STATUS_CHOICES,
        default=ReviewWorkflow.STATUS_PENDING,
    )
    reviewer_notes = models.TextField(blank=True)
    submitted_at = models.DateTimeField(auto_now_add=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="reviewed_listing_edits",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-submitted_at"]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"Edit of {self.target} ({self.status})"

    @property
    def target(self) -> HardwareListing:
        return self.listing_system or self.listing_component  # type: ignore[return-value]

    # --- state transitions --------------------------------------------------
    _COPIED_FIELDS = ("name", "model_number", "description", "vendor_spec_url")

    def approve(self, *, by) -> None:
        self._require_open("approve")
        target = self.target
        changed: list[str] = []
        for field in self._COPIED_FIELDS:
            new = getattr(self, field)
            if new and getattr(target, field) != new:
                setattr(target, field, new)
                changed.append(field)
        if changed:
            target.save(update_fields=changed)
        self.status = self.STATUS_APPROVED
        self.reviewed_by = by
        self.reviewed_at = timezone.now()
        self.save(update_fields=["status", "reviewed_by", "reviewed_at"])

    # reject / request_changes come from ReviewWorkflow.


class CommunityAttestation(models.Model):
    """One piece of approved evidence that a listing works.

    Split out from Submission so the listing detail page can render a compact
    "who has confirmed this works" view without pulling full submission rows.

    Evidence comes from exactly one of two sources: a human submission, or an
    approved certification-suite validation run. Both count as one independent
    confirmation.
    """

    # FK, not OneToOne, for the same reason ``test_run`` below is: one submission
    # now attests each AlmaLinux release it cites, so it produces several rows.
    submission = models.ForeignKey(
        Submission,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="attestations",
    )
    # FK, not OneToOne: a validation run on a custom build attests each
    # component it exercised (motherboard, CPU), so one run can produce
    # several attestation rows against different listings.
    test_run = models.ForeignKey(
        "results.TestRun",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="attestations",
    )
    # The trust tier this piece of evidence counted as: who ran/filed it -
    # community member, verified vendor, or AlmaLinux itself. Fixed at
    # approval time from the submitter's standing; displayed alongside the
    # evidence so readers can weigh it.
    level = models.CharField(
        max_length=16,
        choices=ValidationLevel.choices,
        default=ValidationLevel.COMMUNITY,
    )
    # The AlmaLinux major this evidence is about. An attestation is a statement
    # that the hardware works on a *specific* release, so the version row is what
    # it belongs to; the listing FKs below are denormalized from it.
    version = models.ForeignKey(
        ListingVersion, on_delete=models.CASCADE, related_name="attestations",
    )
    # Denormalized from ``version.listing_system`` / ``version.listing_component``
    # so the many per-listing queries stay single-table. ``version`` is
    # authoritative; these are written from it at creation and not edited after.
    listing_system = models.ForeignKey(
        System, on_delete=models.CASCADE, null=True, blank=True, related_name="attestations"
    )
    listing_component = models.ForeignKey(
        Component, on_delete=models.CASCADE, null=True, blank=True, related_name="attestations"
    )
    # A real column, not the property this replaces. The one-per-person-per-major
    # rule has to be a database constraint, and a constraint cannot reference a
    # value derived through two nullable FKs.
    attested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="hardware_attestations",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                name="attestation_exactly_one_source",
                condition=(
                    models.Q(submission__isnull=False, test_run__isnull=True)
                    | models.Q(submission__isnull=True, test_run__isnull=False)
                ),
            ),
            # One counted confirmation per person per major. Unconditional, so
            # unlike ListingVersion's release uniques this is enforced on MariaDB
            # too - that backend skips conditional constraints (models.W036).
            models.UniqueConstraint(
                fields=["version", "attested_by"],
                name="unique_attestation_per_version_per_user",
            ),
        ]

    def __str__(self) -> str:
        listing = self.listing_system or self.listing_component
        return f"{self.attested_by or 'unknown'} attests {listing}"

    @property
    def source(self) -> str:
        return "submission" if self.submission_id else "test_run"


# --- which FK column points at this listing ------------------------------------
#
# ``System`` and ``Component`` are separate tables, so everything that references a
# listing carries two nullable FKs and picks one. That choice was spelled out at
# eleven sites in four modules, in three shapes: a dict comprehension keyed on
# ``isinstance``, a bare name for a lookup string, and a boolean ``is_system`` flag
# threaded through two functions in ``results.services`` that then rebuilt the dict
# anyway. One of the three had already been extracted (``filters._listing_fk_field``,
# keyed on the model rather than the instance), which is what made the duplication
# visible.


def listing_fk_name(listing) -> str:
    """The FK field name for this listing, e.g. ``"listing_system"``.

    Accepts an instance or a model class, because callers have one or the other:
    services hold a listing, filters hold ``System``/``Component``.
    """
    model = listing if isinstance(listing, type) else type(listing)
    if issubclass(model, System):
        return "listing_system"
    if issubclass(model, Component):
        return "listing_component"
    raise TypeError(f"Not a hardware listing: {listing!r}")


def listing_fk(listing) -> dict:
    """``{fk_name: listing}``, ready to splat into a queryset or ``create``."""
    return {listing_fk_name(listing): listing}


class ComponentNamingRule(models.Model):
    """How a reported part becomes the name it is catalogued and counted under.

    Naming used to be constants in ``results.component_match``: the AMD APU rule that reads an
    iGPU's product name out of the CPU brand string, the list of names that identify nothing, the
    bracket preference. Each was written for a machine somebody had in front of them, and the next
    unidentifiable part meant a release. An Ampere Altra carries its model in the DMI processor
    structure's Part Number, and there was no way to say so.

    A rule is a match and a template. Everything set on the match must hold; a blank field matches
    anything. The template renders in Jinja's sandbox against an allowlisted view of the run (see
    ``results.naming``), so a rule can compose a name out of any field the collector recorded
    without being able to reach anything else.

    Ordered by ``priority``, lowest first, and the first rule whose template produces a non-empty
    name wins. Two enabled rules with the same priority and the same match are refused rather than
    silently resolved, because an ambiguous pair is a bug being written down.
    """

    kind = models.CharField(
        max_length=16, blank=True, choices=ComponentKind.choices(),
        help_text="Restrict to one component kind. Blank matches any.",
    )
    match_field = models.CharField(
        max_length=200, blank=True,
        help_text="Which field the pattern below runs against. Blank means the part's own model. "
                  "Any path a template can read, e.g. cpu.model.",
    )
    model_pattern = models.CharField(
        max_length=200, blank=True,
        help_text=r"Regular expression the field above must match, case-insensitive (e.g. ^Altra). "
                  r"Its captures are what the template reads as match.1, match.2, which is why "
                  r"this is a field of its own rather than a condition.",
    )
    conditions = models.JSONField(
        default=list, blank=True,
        help_text='Everything else the part must satisfy, all of it: [{"field": "pci.vendor_id", '
                  '"op": "eq", "value": "8086"}]. Any field the template can read can be tested, '
                  "with any operator, which is why vendor and the PCI ids are not fields here: "
                  "they were three of the available hundreds, and being fields made them look "
                  "like the only three.",
    )
    build = models.TextField(
        help_text="Jinja template for the name, e.g. "
                  "{{ dmi.processor[0]['Part Number'] }}.",
    )
    fallback = models.TextField(
        blank=True,
        help_text="Used when the template above renders empty - the machine did not report the "
                  "field it needs. Compose something that still distinguishes the part, e.g. "
                  "{{ model }} ({{ cpu.cores }}-core).",
    )
    priority = models.IntegerField(
        default=100,
        help_text="Lowest first. Leave room between rules (10, 20, 30) so one can be inserted "
                  "between two later without renumbering.",
    )
    built_in = models.BooleanField(
        default=False,
        help_text="Shipped with lumina, reproducing behaviour that used to be in code. Editable, "
                  "but a change here changes how every machine of that shape is named.",
    )
    notes = models.CharField(
        max_length=300, blank=True, help_text="Why this rule exists, for whoever reads it next.",
    )
    enabled = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["priority", "id"]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.kind or 'any'} naming rule #{self.pk} (priority {self.priority})"

    @property
    def match_signature(self) -> tuple:
        """What this rule matches, for spotting two rules that cannot be told apart."""
        return (
            self.kind, self.match_field, self.model_pattern,
            json.dumps(sorted((self.conditions or []), key=json.dumps), sort_keys=True),
        )

    @override
    def clean(self) -> None:
        from django.core.exceptions import ValidationError

        from lumina.results import naming

        if self.model_pattern:
            try:
                re.compile(self.model_pattern)
            except re.error as exc:
                raise ValidationError(
                    {"model_pattern": f"Not a regular expression: {exc}"}) from exc
        for field in ("build", "fallback"):
            problem = naming.template_error(getattr(self, field) or "")
            if problem:
                raise ValidationError({field: problem})
        problem = naming.conditions_error(self.conditions or [])
        if problem:
            raise ValidationError({"conditions": problem})
        if not (self.build or "").strip():
            raise ValidationError({"build": "A rule with no template names nothing."})
        clash = (
            type(self).objects.filter(enabled=True, priority=self.priority)
            .exclude(pk=self.pk)
        )
        for other in clash:
            if other.match_signature == self.match_signature:
                raise ValidationError({"priority": (
                    f"Rule #{other.pk} already matches exactly this at priority "
                    f"{self.priority}. Give one of them a different priority, or change what "
                    f"they match: which of the two applies would otherwise be undefined."
                )})


class NameSuggestionField(models.Model):
    """A field worth offering a submitter when they say a part is misidentified.

    The submitter is not asked which *field* holds the real model - that would be asking them to
    understand the payload. They are shown a short list of values their own machine reported and
    pick one; these rows decide which values make that list, and what each is called.

    A row per kind and path, because what identifies a part differs by kind and by machine: an
    Ampere Altra carries its model in the DMI processor structure's part number, a Supermicro
    board carries the useful name on the baseboard rather than the system, and an NVIDIA card
    knows its own marketing name through nvidia-smi. A machine that hides its identity somewhere
    new is a row somebody adds rather than a release.

    ``path`` addresses the same context a naming rule reads, with ``*`` standing in for a list
    index: ``dmi.processor.*.Part Number``. Whatever a chosen value turns out to be, the path it
    came from is recorded with it, which is what lets a reviewer turn the answer into a rule.
    """

    kind = models.CharField(
        max_length=16, choices=ComponentKind.choices(),
        help_text="The component kind this field is offered for.",
    )
    path = models.CharField(
        max_length=200,
        help_text="Where the value is, in the same paths a naming rule reads. "
                  "Use * for a list index: dmi.processor.*.Part Number",
    )
    label = models.CharField(
        max_length=80,
        help_text="What to call it when it is offered, e.g. \"Processor part number\". Shown "
                  "under the value, so it only has to tell two similar values apart.",
    )
    priority = models.IntegerField(
        default=100, help_text="Lowest first, which is the order they are offered in.",
    )
    enabled = models.BooleanField(default=True)
    built_in = models.BooleanField(
        default=False,
        help_text="Shipped with lumina. Editable, like any other row.",
    )

    class Meta:
        ordering = ["kind", "priority", "id"]
        constraints = [
            models.UniqueConstraint(fields=["kind", "path"], name="unique_suggestion_field"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.kind}: {self.path}"

    @override
    def clean(self) -> None:
        from django.core.exceptions import ValidationError

        if not (self.path or "").strip():
            raise ValidationError({"path": "A row with no path offers nothing."})
        if self.path.strip(".") != self.path or ".." in self.path:
            raise ValidationError({"path": "Not a path: it has an empty segment."})


class ComponentExclusionRule(models.Model):
    """A PCI device the catalog should not auto-attach as a component - a BMC display adapter, an
    onboard iGPU nobody certifies, and the like.

    A matched device is still kept verbatim in the run's inventory (the collector reports
    everything); it is only unticked by default on the review screen, flagged with this rule's
    ``reason``, and a reviewer can tick it to include it anyway. Curated here rather than in code so
    a newly-noticed piece of junk is one admin row, not a deploy.

    All set fields must match; a blank field matches anything. ``vendor_id`` + ``device_id`` pins
    one exact part; ``vendor_id`` alone covers a whole vendor; ``kind`` narrows to one component
    kind. A row with none of the three set would match everything and is rejected. The blanket
    "a display adapter from a vendor that makes no accelerators is a management/BMC console" rule is
    applied in code (``results.exclusions``), so ASPEED and Matrox need no row here.
    """

    vendor_id = models.CharField(
        max_length=4, blank=True,
        help_text="PCI vendor id, 4 hex digits (e.g. 1a03). Blank matches any vendor.",
    )
    device_id = models.CharField(
        max_length=4, blank=True,
        help_text="PCI device id, 4 hex digits (e.g. 164e). Blank matches any device of the vendor.",
    )
    kind = models.CharField(
        max_length=16, blank=True, choices=ComponentKind.choices(),
        help_text="Restrict to one component kind. Blank matches any.",
    )
    reason = models.CharField(
        max_length=200,
        help_text="Shown beside the excluded device on the review screen, e.g. "
                  "'onboard iGPU, not the accelerator under test'.",
    )
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["vendor_id", "device_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["vendor_id", "device_id", "kind"],
                name="unique_component_exclusion_rule",
            ),
        ]

    def __str__(self) -> str:
        target = ":".join(part for part in (self.vendor_id, self.device_id) if part) or "any"
        return f"exclude {self.kind or 'any'} {target}"

    def save(self, *args, **kwargs):
        # Stored lowercase so matching against lspci's lowercase hex is a plain comparison.
        self.vendor_id = (self.vendor_id or "").strip().lower()
        self.device_id = (self.device_id or "").strip().lower()
        super().save(*args, **kwargs)
