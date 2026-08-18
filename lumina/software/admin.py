"""Admin registration for the software catalog.

Day-to-day review happens at ``/review/``; this exists for data fixes and for
recording an AlmaLinux certification against a major, which has no public form.

Simpler than ``hardware/admin.py`` in one specific way: there is a single listing
model, so the inlines each have exactly one FK to disambiguate and none of
hardware's ``get_inline_instances`` / ``fk_name`` pinning is needed.
"""
from __future__ import annotations

from typing import override

from django.contrib import admin
from unfold.admin import ModelAdmin, TabularInline

from lumina.software.models import (
    Software,
    SoftwareAttestation,
    SoftwareCategoryValue,
    SoftwareCertification,
    SoftwareCompatibility,
    SoftwareEditProposal,
    SoftwareEvidenceAttachment,
    SoftwareSubmission,
)


class SoftwareCompatibilityInline(TabularInline):
    model = SoftwareCompatibility
    extra = 0
    fields = ("release", "status", "validation_level", "proposed_by")
    readonly_fields = ("validation_level",)
    autocomplete_fields = ("release",)


class SoftwareCategoryValueInline(TabularInline):
    model = SoftwareCategoryValue
    extra = 0
    autocomplete_fields = ("value",)


@admin.register(Software)
class SoftwareAdmin(ModelAdmin):
    list_display = (
        "name", "vendor", "owner_vendor", "validation_level", "published",
    )
    list_filter = ("published", "validation_level", "vendor")
    search_fields = ("name", "slug")
    autocomplete_fields = ("vendor", "owner_vendor", "created_by")
    prepopulated_fields = {"slug": ("name",)}
    inlines = [SoftwareCompatibilityInline, SoftwareCategoryValueInline]
    # Derived from the per-major certifications; editing it here would be
    # overwritten by the next recompute.
    readonly_fields = ("validation_level",)
    fieldsets = (
        (None, {"fields": ("name", "vendor", "owner_vendor", "slug")}),
        ("Catalog listing", {
            "fields": ("description", "homepage_url", "documentation_url",
                       "support_url", "published", "validation_level"),
        }),
    )


class SoftwareCertificationInline(TabularInline):
    model = SoftwareCertification
    extra = 0
    fields = ("level", "certified_by", "notes")
    autocomplete_fields = ("certified_by",)


class SoftwareAttestationInline(TabularInline):
    """Read-only: community confirmations are earned through the public control,
    not typed in here. Deleting one is still possible, which is the moderation
    path, and the tier recomputes because the delete goes through the model."""

    model = SoftwareAttestation
    extra = 0
    fields = ("user", "note", "created_at")
    readonly_fields = ("user", "note", "created_at")
    can_delete = True

    @override
    def has_add_permission(self, request, obj) -> bool:
        return False


@admin.register(SoftwareCompatibility)
class SoftwareCompatibilityAdmin(ModelAdmin):
    list_display = ("software", "release", "validation_level", "status",
                    "attestation_count", "proposed_by")
    list_filter = ("status", "validation_level", "release")
    search_fields = ("software__name",)
    autocomplete_fields = ("software", "release", "proposed_by", "reviewed_by")
    readonly_fields = ("validation_level",)
    inlines = [SoftwareCertificationInline, SoftwareAttestationInline]

    @admin.display(description="Confirmations")
    def attestation_count(self, obj: SoftwareCompatibility) -> int:
        return obj.attestations.count()

    @override
    def delete_queryset(self, request, queryset) -> None:
        # Bulk delete skips each row's own delete(), so the products whose tiers
        # depended on those rows would keep a stale badge.
        affected = {row.software for row in queryset.select_related("software")}
        super().delete_queryset(request, queryset)
        for software in affected:
            software.recompute_levels()


class SoftwareEvidenceAttachmentInline(TabularInline):
    model = SoftwareEvidenceAttachment
    extra = 0
    readonly_fields = ("sha256", "uploaded_at")


@admin.register(SoftwareSubmission)
class SoftwareSubmissionAdmin(ModelAdmin):
    list_display = ("uuid", "software", "submitter", "on_behalf_of",
                    "claimed_validation_level", "status", "submitted_at")
    list_filter = ("status", "claimed_validation_level")
    search_fields = ("uuid", "software__name", "submitter__username")
    autocomplete_fields = ("software", "submitter", "on_behalf_of", "reviewed_by")
    readonly_fields = ("uuid", "submitted_at", "reviewed_at")
    inlines = [SoftwareEvidenceAttachmentInline]


@admin.register(SoftwareEditProposal)
class SoftwareEditProposalAdmin(ModelAdmin):
    list_display = ("__str__", "proposed_by", "status", "submitted_at", "reviewed_at")
    list_filter = ("status",)
    search_fields = ("software__name", "proposed_by__username")
    autocomplete_fields = ("software", "proposed_by", "reviewed_by")
    readonly_fields = ("submitted_at", "reviewed_at")
