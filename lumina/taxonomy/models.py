"""Taxonomy: admin-curated categories and their values.

A ``Category`` describes one filter axis on the hardware catalog (e.g.
Architecture, Network, Management, Certified-for-AlmaLinux-version,
Provider/Vendor for hardware-agnostic contexts).

A ``CategoryValue`` is a single choice within a category. Values may be:

- **approved** - created by an admin, or a pending value later promoted.
- **pending** - proposed by a submitter via ``CategoryValue.propose``; only
  visible on its originating submission until a reviewer approves.
- **rejected** - reviewer said no; kept for audit but excluded from listings.

Only approved values appear in public filter UIs. The review tooling uses the
full queryset to surface pending values for curation.
"""
from __future__ import annotations

from enum import StrEnum
from typing import override

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.text import slugify


class PickerWidget(StrEnum):
    """How a Category renders on the submission form.

    - ``dropdown`` is single-select. A listing may have at most one value.
    - ``checkboxes`` is multi-select rendered as a checkbox grid (good for
      short lists where all options should be visible at a glance).
    - ``multiselect`` is multi-select rendered as a scrollable HTML
      ``<select multiple>`` listbox (better for long lists where the
      checkbox grid would dominate the page).
    """

    dropdown = "dropdown"
    checkboxes = "checkboxes"
    multiselect = "multiselect"


class Category(models.Model):
    APPLIES_SYSTEM = "system"
    APPLIES_COMPONENT = "component"
    APPLIES_BOTH = "both"
    # Software categories are a separate vocabulary from hardware's - Backup and
    # Analytics have nothing to do with Architecture - so they get their own scope
    # rather than being folded into ``both``, whose meaning stays "systems and
    # components".
    APPLIES_SOFTWARE = "software"
    APPLIES_CHOICES = [
        (APPLIES_SYSTEM, "Systems only"),
        (APPLIES_COMPONENT, "Components only"),
        (APPLIES_BOTH, "Systems and Components"),
        (APPLIES_SOFTWARE, "Software only"),
    ]

    name = models.CharField(max_length=80, unique=True)
    slug = models.SlugField(max_length=100, unique=True)
    applies_to = models.CharField(
        max_length=16, choices=APPLIES_CHOICES, default=APPLIES_BOTH
    )
    description = models.TextField(blank=True)
    # Populated from validation-run evidence rather than by a submitter, so the
    # submit forms leave it out entirely.
    #
    # Architecture is the case this exists for: every run reports the kernel's
    # arch, which makes an approved run authoritative and a submitter's answer
    # merely an opinion that could contradict it. The three facets removed
    # alongside this - Network, Storage, PCIe Generation - failed the opposite
    # test: nothing filled them in reliably, so they were blank often enough that
    # filtering on them hid working hardware.
    derived_from_runs = models.BooleanField(
        default=False,
        help_text=(
            "Set from an approved validation run's own report. Hidden from the "
            "submit forms, because the machine is the authority on it."
        ),
    )
    collapsed_limit = models.PositiveIntegerField(
        default=settings.LUMINA_DEFAULT_COLLAPSED_LIMIT,
        help_text=(
            "Number of values shown before the 'Expand + search' control "
            "appears in the filter panel."
        ),
    )
    picker_widget = models.CharField(
        max_length=16,
        choices=[(w.value, w.value.capitalize()) for w in PickerWidget],
        default=PickerWidget.checkboxes.value,
        help_text=(
            "How this category is rendered on the submission form. "
            "Use 'dropdown' for axes where a listing has at most one value "
            "(e.g. Architecture). 'multiselect' suits long lists; "
            "'checkboxes' suits short ones where every option should be "
            "visible at a glance."
        ),
    )
    allow_suggestions = models.BooleanField(
        default=True,
        help_text=(
            "If True, submitters may propose a new value for this category "
            "via an inline 'Propose new …' input. Disable for curated axes "
            "(e.g. AlmaLinux versions, which only the Foundation can release)."
        ),
    )
    display_order = models.PositiveIntegerField(default=100)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["display_order", "name"]
        verbose_name_plural = "Categories"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.name


class CategoryValueQuerySet(models.QuerySet):
    def approved(self) -> CategoryValueQuerySet:
        return self.filter(status=CategoryValue.STATUS_APPROVED)

    def pending(self) -> CategoryValueQuerySet:
        return self.filter(status=CategoryValue.STATUS_PENDING)


class CategoryValue(models.Model):
    STATUS_PENDING = "pending"
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_REJECTED, "Rejected"),
    ]

    category = models.ForeignKey(
        Category, on_delete=models.CASCADE, related_name="values"
    )
    value = models.CharField(max_length=120)
    slug = models.SlugField(max_length=140, blank=True)
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_APPROVED
    )
    description = models.TextField(blank=True)
    proposed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="proposed_taxonomy_values",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_taxonomy_values",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = CategoryValueQuerySet.as_manager()

    class Meta:
        ordering = ["category__display_order", "value"]
        unique_together = [("category", "value")]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.category.name}: {self.value}"

    @override
    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.value)[:140]
        super().save(*args, **kwargs)

    @property
    def is_approved(self) -> bool:
        """Instance-level twin of ``CategoryValueQuerySet.approved()``, for callers holding a value
        (or a plain list of them) rather than a queryset - e.g. the ``approved_values_only`` template
        filter, which receives an already-evaluated list."""
        return self.status == self.STATUS_APPROVED

    # --- Workflow ------------------------------------------------------------
    @classmethod
    def propose(
        cls, *, category: Category, value: str, proposed_by, description: str = ""
    ) -> CategoryValue:
        return cls.objects.create(
            category=category,
            value=value,
            description=description,
            status=cls.STATUS_PENDING,
            proposed_by=proposed_by,
        )

    def approve(self, *, by) -> None:
        if self.status == self.STATUS_APPROVED:
            raise ValueError("CategoryValue is already approved.")
        self.status = self.STATUS_APPROVED
        self.approved_by = by
        self.approved_at = timezone.now()
        self.save(update_fields=["status", "approved_by", "approved_at"])

    def reject(self, *, by) -> None:
        if self.status == self.STATUS_REJECTED:
            raise ValueError("CategoryValue is already rejected.")
        self.status = self.STATUS_REJECTED
        self.approved_by = by  # reviewer-of-record, even for rejection
        self.approved_at = timezone.now()
        self.save(update_fields=["status", "approved_by", "approved_at"])
