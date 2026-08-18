"""Admin registration for results models.

Day-to-day review happens in /review/ - the admin is for debugging ingests
and data surgery, so everything is read-mostly here.
"""
from __future__ import annotations

from django.contrib import admin
from unfold.admin import ModelAdmin, TabularInline

from lumina.results.models import (
    BenchmarkResult,
    GenericModel,
    ReportedIdentityAlias,
    RunArtifact,
    TestResult,
    TestRun,
)


class TestResultInline(TabularInline):
    model = TestResult
    extra = 0
    can_delete = False
    readonly_fields = ["test_id", "category", "severity", "status", "reason", "duration_ms"]
    fields = readonly_fields


class BenchmarkResultInline(TabularInline):
    model = BenchmarkResult
    extra = 0
    can_delete = False
    readonly_fields = ["benchmark_id", "benchmark_version", "metric", "value", "unit",
                       "direction", "is_primary"]
    fields = readonly_fields


@admin.register(TestRun)
class TestRunAdmin(ModelAdmin):
    list_display = [
        "uuid", "run_type", "status", "system_vendor", "system_product",
        "cpu_model", "alma_release", "pre_release", "published_at", "received_at",
    ]
    list_filter = ["run_type", "status", "pre_release", "target_type", "source"]
    search_fields = ["uuid", "cpu_model", "system_vendor", "system_product", "gpu_model"]
    readonly_fields = [
        "uuid", "schema_version", "suite_version", "suite_git_commit", "bundle_sha256",
        "bundle_size", "inventory", "environment", "received_at",
    ]
    inlines = [TestResultInline, BenchmarkResultInline]


@admin.register(RunArtifact)
class RunArtifactAdmin(ModelAdmin):
    list_display = ["run", "bundle_path", "size", "sha256"]
    search_fields = ["bundle_path", "run__uuid"]


@admin.register(ReportedIdentityAlias)
class ReportedIdentityAliasAdmin(ModelAdmin):
    """Review and correct the firmware-string-to-listing mappings.

    Unlike the rest of this module these are editable, because they are a
    judgement rather than ingested data: a mapping recorded from one run's
    review decides how every future run of that machine is classified, so a
    wrong one keeps being wrong until somebody fixes it here.

    Adding one by hand is equally valid - a fleet whose firmware reports a
    machine-type code can be mapped before its first run is ever submitted.
    """

    list_display = (
        "reported_vendor", "reported_product", "listing_display", "resolved_kind",
        "origin", "created_by", "created_at",
    )
    list_filter = ("resolved_kind", "created_at")
    search_fields = (
        "reported_vendor", "reported_product",
        "listing_system__name", "listing_component__name",
    )
    autocomplete_fields = ("listing_system", "listing_component", "created_by")
    readonly_fields = ("created_at",)
    fieldsets = (
        ("Reported by the firmware", {
            "description": (
                "The strings a run arrives with, matched case-insensitively. "
                "The vendor may be blank - unbranded firmware reports none, "
                "which is exactly when a mapping is needed."
            ),
            "fields": ("reported_vendor", "reported_product"),
        }),
        ("Means this listing", {
            "description": (
                "Set <strong>one</strong>. A vendor system maps to a System; a "
                "custom build is identified by its motherboard, so map those to "
                "a Component."
            ),
            "fields": ("listing_system", "listing_component", "resolved_kind"),
        }),
        ("Provenance", {
            "fields": ("source_run", "created_by", "notes", "created_at"),
        }),
    )

    @admin.display(description="Means")
    def listing_display(self, obj) -> str:
        listing = obj.listing
        return str(listing) if listing else "-"

    @admin.display(description="Origin")
    def origin(self, obj) -> str:
        return "run review" if obj.source_run_id else "entered by hand"


@admin.register(GenericModel)
class GenericModelAdmin(ModelAdmin):
    """Product-Name strings that name a product line, not a model (Supermicro "Super Server").

    Editable because which strings are generic is a judgement about firmware, like the identity
    aliases. A machine whose Product Name matches one of these is treated as identified by its
    motherboard, and the submitter is asked for the real vendor model if one exists.
    """

    list_display = ("vendor", "product", "note", "created_by", "created_at")
    search_fields = ("vendor", "product", "note")
    autocomplete_fields = ("created_by",)
    readonly_fields = ("created_at",)
