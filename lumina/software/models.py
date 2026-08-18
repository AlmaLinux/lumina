"""The software certification catalog.

Deliberately simpler than the hardware side in two ways, and deliberately more
granular in one.

Simpler: there is a single listing model, so none of hardware's
``listing_system`` / ``listing_component`` dual-FK-plus-XOR-constraint pattern
appears here. And AlmaLinux support is tracked per **major** only - no minor
floor, and none of the vendor's own product version numbers - because a software
certification says "this product is certified for AlmaLinux 9" with the
implication that the vendor's ongoing releases keep supporting it.

More granular: validation is a property of each **cited major**, not of the
listing. That is the answer to vendor abandonment. A vendor who certifies for
AlmaLinux 8 and walks away leaves a listing whose 8 row is vendor-certified and
whose 10 row either does not exist or carries only community confirmations, and
the page says exactly that.

``Software.validation_level`` still exists, as a denormalized rollup, because the
browse card's colour, the level filter, and the level ordering all need one value
per row. It is the highest tier across the product's approved majors.
"""
from __future__ import annotations

import uuid as uuid_lib
from pathlib import Path
from typing import override

from django.conf import settings
from django.db import models
from django.utils import timezone

from lumina.core.certification import (
    ValidationLevel,
    highest_level,
    resync_derived_levels,
)
from lumina.core.models import VendorSlugMixin
from lumina.core.review import ReviewWorkflow

# The two tiers a certification row can record. Community standing is not a
# certification - it is earned by attestations - so it is absent here.
CERTIFIABLE_LEVELS = [
    (ValidationLevel.VENDOR.value, ValidationLevel.VENDOR.label),
    (ValidationLevel.ALMALINUX.value, ValidationLevel.ALMALINUX.label),
]

def _evidence_upload_to(instance: SoftwareEvidenceAttachment, filename: str) -> str:
    return str(Path("software-evidence") / str(instance.submission.uuid) / filename)


class SoftwareQuerySet(models.QuerySet["Software"]):
    def published(self) -> SoftwareQuerySet:
        return self.filter(published=True)


class Software(VendorSlugMixin, models.Model):
    """One software product. The catalog's only software listing model."""

    vendor = models.ForeignKey(
        "vendors.Vendor", on_delete=models.PROTECT, related_name="+",
        help_text="Who publishes it.",
    )
    owner_vendor = models.ForeignKey(
        "vendors.Vendor", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="owned_software",
        help_text=(
            "Vendor responsible for keeping this entry accurate; drives edit "
            "rights. Null means community-submitted and nobody has claimed it."
        ),
    )
    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=220, unique=True, blank=True)
    description = models.TextField(blank=True)
    homepage_url = models.URLField(blank=True)
    documentation_url = models.URLField(blank=True)
    support_url = models.URLField(blank=True)

    published = models.BooleanField(default=False)
    # Denormalized rollup of the per-major tiers. Maintained by
    # ``recompute_levels``; never set this directly.
    validation_level = models.CharField(
        max_length=16, choices=ValidationLevel.choices,
        default=ValidationLevel.COMMUNITY, db_index=True,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SoftwareQuerySet.as_manager()

    class Meta:
        ordering = ["name"]
        # Django would derive "softwares".
        verbose_name_plural = "software"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.vendor} {self.name}"

    def get_absolute_url(self) -> str:
        from django.urls import reverse

        return reverse("software:detail", args=[self.slug])

    def recompute_levels(self) -> None:
        """Re-derive every per-major tier and the listing rollup.

        Called from the certification model's own save and delete, so the derived
        columns cannot be left stale by a caller that forgot - including an admin
        deleting a row inline. Cheap: a product cites a handful of majors.

        Only **approved** majors feed the rollup. A community-reported major
        still awaiting review must not promote the product's badge.
        """
        rows = list(self.compatibility.prefetch_related("certifications"))
        resync_derived_levels(rows)

        rollup = highest_level(
            row.validation_level for row in rows
            if row.status == SoftwareCompatibility.STATUS_APPROVED
        )
        if self.validation_level != rollup:
            self.validation_level = rollup
            self.save(update_fields=["validation_level"])


class SoftwareCompatibilityQuerySet(models.QuerySet["SoftwareCompatibility"]):
    def approved(self) -> SoftwareCompatibilityQuerySet:
        return self.filter(status=SoftwareCompatibility.STATUS_APPROVED)

    def pending(self) -> SoftwareCompatibilityQuerySet:
        return self.filter(status=SoftwareCompatibility.STATUS_PENDING)


class SoftwareCompatibility(models.Model):
    """One AlmaLinux major a product is cited for. The unit of validation.

    A plain M2M would not do, because this row carries a tier and a review state.
    It is not hardware's ``ListingVersion`` either: there is no ``source`` (there is no
    automated evidence path for software, so every row would say "declared"). Hardware used
    to differ on a second count, a ``minimum_minor`` floor, and no longer does - both
    catalogs certify per major.

    ``status`` exists for the case where the *community* cites a major the vendor
    never did - when AlmaLinux 11 ships and a user reports that a product works
    on it. Adding a major is reviewed once; agreeing with it afterwards is one
    click. The shape mirrors ``taxonomy.CategoryValue``, which is the house
    pattern for a user-proposed row awaiting review.

    There is no ``rejected`` status: rejecting a community report **deletes** the
    row, so that one bad early report cannot permanently block a genuine one
    later, and a dead status value would be unreachable.
    """

    STATUS_PENDING = "pending"
    STATUS_APPROVED = "approved"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Awaiting review"),
        (STATUS_APPROVED, "Approved"),
    ]

    software = models.ForeignKey(
        Software, on_delete=models.CASCADE, related_name="compatibility"
    )
    release = models.ForeignKey(
        "releases.AlmaLinuxRelease", on_delete=models.PROTECT,
        related_name="software_compatibility",
    )
    # Derived from this row's certifications by ``Software.recompute_levels``.
    validation_level = models.CharField(
        max_length=16, choices=ValidationLevel.choices,
        default=ValidationLevel.COMMUNITY,
    )
    # Its own two-value vocabulary, deliberately NOT ``ReviewWorkflow``'s four.
    # A cited major is pending or approved; there is no rejected (rejecting deletes
    # the row, see the class docstring) and no needs-changes (there is nothing for a
    # submitter to revise about a major number). This class is the reason
    # ``ReviewWorkflow`` is opt-in rather than applied to everything with a status.
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_APPROVED
    )
    proposed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="proposed_software_majors",
        help_text="Set only when a community member cited this major.",
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="reviewed_software_majors",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    objects = SoftwareCompatibilityQuerySet.as_manager()

    class Meta:
        ordering = ["-release__major"]
        verbose_name_plural = "software compatibility"
        constraints = [
            models.UniqueConstraint(
                fields=["software", "release"], name="one_row_per_software_and_major",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.software} on {self.release}"

    @classmethod
    def propose(cls, *, software, release, proposed_by) -> SoftwareCompatibility:
        """Cite a major on someone else's listing, pending review."""
        return cls.objects.create(
            software=software, release=release, proposed_by=proposed_by,
            status=cls.STATUS_PENDING,
        )

    def derived_level(self) -> str:
        """This major's tier, from its certifications.

        Delegates to ``highest_level`` rather than ranking here. This method used to
        hand-roll a second ordering that put AlmaLinux above vendor, left over from
        when the two shared rank 1 and a tie preference decided the badge. When the
        tiers became a total order with vendor on top
        (``core.certification.LEVEL_RANK``) this copy was missed, so a major holding
        both a vendor and an AlmaLinux certification reported ``almalinux`` while
        ``highest_level`` said ``vendor`` - and the wrong value persisted into the
        row, the product's rollup badge, the API, and the admin list.

        Community is the floor rather than a claim, because a row only exists once
        something cited it; ``highest_level`` already floors an empty input there.
        Both badges still render on the detail page, so nothing is hidden by the
        single value this returns.
        """
        return highest_level(c.level for c in self.certifications.all())

    def approve(self, *, by) -> None:
        """Accept a community-reported major."""
        if self.status != self.STATUS_PENDING:
            raise ValueError("Only a pending major can be approved.")
        self.status = self.STATUS_APPROVED
        self.reviewed_by = by
        self.reviewed_at = timezone.now()
        self.save(update_fields=["status", "reviewed_by", "reviewed_at"])
        self.software.recompute_levels()


class SoftwareCertification(models.Model):
    """Who validated a product on one AlmaLinux major.

    One row per (major, validator), so a major can be vendor-certified and
    AlmaLinux-certified simultaneously, and both stay visible.
    """

    compatibility = models.ForeignKey(
        SoftwareCompatibility, on_delete=models.CASCADE, related_name="certifications"
    )
    level = models.CharField(max_length=16, choices=CERTIFIABLE_LEVELS)
    certified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="software_certifications",
    )
    certified_at = models.DateTimeField(auto_now_add=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["level"]
        constraints = [
            models.UniqueConstraint(
                fields=["compatibility", "level"],
                name="one_certification_per_major_and_validator",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.get_level_display()}: {self.compatibility}"

    @override
    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # Here rather than in a service so the derived tiers cannot go stale
        # through the admin, a shell, or a caller that forgot.
        self.compatibility.software.recompute_levels()

    @override
    def delete(self, *args, **kwargs):
        software = self.compatibility.software
        super().delete(*args, **kwargs)
        # Derived, not stored-and-raised: losing a
        # certification really is a downgrade, and the badge should say so.
        software.recompute_levels()


class SoftwareAttestation(models.Model):
    """One community member confirming a product works on one major.

    Capped at one per user per major, and added in a single click with no detail
    asked - the whole point is that agreeing is cheap. Withdrawing is deleting
    the row.
    """

    compatibility = models.ForeignKey(
        SoftwareCompatibility, on_delete=models.CASCADE, related_name="attestations"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="software_attestations",
    )
    note = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["compatibility", "user"],
                name="one_attestation_per_user_and_major",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.user} confirms {self.compatibility}"


class SoftwareCategoryValue(models.Model):
    """Binds a product to a taxonomy value.

    The software app owns its own join table so hardware's
    ``ListingCategoryValue`` does not grow a third nullable FK; the ``Category``
    and ``CategoryValue`` vocabulary itself is shared.
    """

    software = models.ForeignKey(
        Software, on_delete=models.CASCADE, related_name="category_values"
    )
    value = models.ForeignKey(
        "taxonomy.CategoryValue", on_delete=models.PROTECT,
        related_name="software_bindings",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["software", "value"], name="unique_software_value",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.software}: {self.value}"


class SoftwareSubmission(ReviewWorkflow, models.Model):
    """A request to publish or re-validate a software listing."""

    review_noun = "submission"


    uuid = models.UUIDField(default=uuid_lib.uuid4, unique=True, editable=False)
    submitter = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="software_submissions",
    )
    on_behalf_of = models.ForeignKey(
        "vendors.Vendor", on_delete=models.PROTECT, null=True, blank=True,
        related_name="software_submissions",
    )
    software = models.ForeignKey(
        Software, on_delete=models.CASCADE, related_name="submissions"
    )
    claimed_validation_level = models.CharField(
        max_length=16, choices=ValidationLevel.choices,
        default=ValidationLevel.COMMUNITY,
    )
    status = models.CharField(
        max_length=16, choices=ReviewWorkflow.STATUS_CHOICES, default=ReviewWorkflow.STATUS_PENDING
    )
    reviewer_notes = models.TextField(blank=True)
    submitter_notes = models.TextField(blank=True)
    submitted_at = models.DateTimeField(auto_now_add=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="reviewed_software_submissions",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-submitted_at"]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"Submission for {self.software}"

    def approve(self, *, by, final_level: str) -> None:
        """Publish the listing and record the reviewer's tier on every major.

        The majors themselves were stored when the form was submitted - the
        submission row carries no list of them - so this walks what the draft
        listing already cites, exactly as hardware's ``_attach_release_versions``
        runs inside ``form.save()``.

        At the community tier there is nothing to certify, so the submitter's own
        confirmation is recorded instead. That is what makes a community listing's
        count start at one rather than zero.
        """
        self._require_open("approve")

        self.software.published = True
        self.software.save(update_fields=["published"])

        rows = list(self.software.compatibility.all())
        for row in rows:
            if row.status == SoftwareCompatibility.STATUS_PENDING:
                row.status = SoftwareCompatibility.STATUS_APPROVED
                row.reviewed_by = by
                row.reviewed_at = timezone.now()
                row.save(update_fields=["status", "reviewed_by", "reviewed_at"])
            if final_level == ValidationLevel.COMMUNITY:
                SoftwareAttestation.objects.get_or_create(
                    compatibility=row, user=self.submitter,
                )
            else:
                SoftwareCertification.objects.get_or_create(
                    compatibility=row, level=final_level,
                    defaults={"certified_by": by},
                )

        # An inline-proposed vendor was invisible while the submission was
        # pending; the reviewer has now seen it as part of this decision.
        if not self.software.vendor.published:
            self.software.vendor.published = True
            self.software.vendor.save(update_fields=["published"])

        self.software.recompute_levels()

        self._record_approval(by=by)





class SoftwareEvidenceAttachment(models.Model):
    submission = models.ForeignKey(
        SoftwareSubmission, on_delete=models.CASCADE, related_name="attachments"
    )
    file = models.FileField(upload_to=_evidence_upload_to)
    description = models.CharField(max_length=300, blank=True)
    sha256 = models.CharField(max_length=64, blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.description or self.file.name


class SoftwareEditProposal(ReviewWorkflow, models.Model):
    """A pending edit to a vendor-owned software listing.

    Mirrors ``hardware.ListingEditProposal``: proposed values are blank-able
    mirrors of the listing's own fields, and blank means "no change" so a sparse
    form cannot clear something by omission.
    """

    review_noun = "software edit proposal"

    _COPIED_FIELDS = (
        "name", "description", "homepage_url", "documentation_url", "support_url",
    )

    software = models.ForeignKey(
        Software, on_delete=models.CASCADE, related_name="edit_proposals"
    )
    proposed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="software_edit_proposals",
    )
    name = models.CharField(max_length=200, blank=True)
    description = models.TextField(blank=True)
    homepage_url = models.URLField(blank=True)
    documentation_url = models.URLField(blank=True)
    support_url = models.URLField(blank=True)

    submitter_notes = models.TextField(blank=True)
    status = models.CharField(
        max_length=16, choices=ReviewWorkflow.STATUS_CHOICES, default=ReviewWorkflow.STATUS_PENDING
    )
    reviewer_notes = models.TextField(blank=True)
    submitted_at = models.DateTimeField(auto_now_add=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="reviewed_software_edits",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-submitted_at"]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"Edit proposal for {self.software}"

    def approve(self, *, by) -> None:
        self._require_open("approve")
        changed = []
        for field in self._COPIED_FIELDS:
            proposed = getattr(self, field)
            if proposed and proposed != getattr(self.software, field):
                setattr(self.software, field, proposed)
                changed.append(field)
        if changed:
            self.software.save(update_fields=changed)
        self.status = self.STATUS_APPROVED
        self.reviewed_by = by
        self.reviewed_at = timezone.now()
        self.save(update_fields=["status", "reviewed_by", "reviewed_at"])

