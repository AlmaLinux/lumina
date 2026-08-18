"""Survey admin: moderate the raw pool, inspect stats, manage token grants.

``SurveySubmission`` is append-only, so its admin is read-mostly: every column is
read-only except ``review_state``, which is the one moderation lever (and the
bulk actions below are the usual way to pull it). ``SurveyStat`` is derived and
recomputed by the rollup job, so it is view-only here.
"""
from __future__ import annotations

from django.contrib import admin
from unfold.admin import ModelAdmin

from lumina.survey.models import (
    SurveySegment,
    SurveyStat,
    SurveySubmission,
    SurveyTokenGrant,
    SurveyTokenRequest,
)


@admin.register(SurveySubmission)
class SurveySubmissionAdmin(ModelAdmin):
    list_display = ("uuid", "received_at", "origin", "trust_tier", "cpu_model",
                    "virtual", "review_state")
    list_filter = ("origin", "trust_tier", "virtual", "review_state", "arch")
    search_fields = ("cpu_model", "board_model", "gpu_model", "identity_hash")
    date_hierarchy = "received_at"
    actions = ["accept", "dismiss", "return_to_new"]

    def has_add_permission(self, request) -> bool:
        return False  # append-only: records arrive through ingest, never the admin

    def get_readonly_fields(self, request, obj=None):
        # Everything but the one mutable moderation column.
        return [f.name for f in self.model._meta.fields if f.name != "review_state"]

    def save_model(self, request, obj, form, change):
        # Honor the append-only guard: only review_state may be written back.
        obj.save(update_fields=["review_state"])

    @admin.action(description="Accept (reviewed, keep in stats)")
    def accept(self, request, queryset):
        queryset.update(review_state=SurveySubmission.REVIEW_ACCEPTED)

    @admin.action(description="Dismiss (exclude from published stats)")
    def dismiss(self, request, queryset):
        queryset.update(review_state=SurveySubmission.REVIEW_DISMISSED)

    @admin.action(description="Return to New")
    def return_to_new(self, request, queryset):
        queryset.update(review_state=SurveySubmission.REVIEW_NEW)


@admin.register(SurveyStat)
class SurveyStatAdmin(ModelAdmin):
    list_display = ("period", "dimension", "bucket", "count", "tier_scope", "dedup_key")
    list_filter = ("period", "dimension", "tier_scope", "dedup_key")
    search_fields = ("bucket",)

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False  # derived; rebuilt by the survey_rollup job


@admin.register(SurveyTokenGrant)
class SurveyTokenGrantAdmin(ModelAdmin):
    list_display = ("user", "granted_by", "granted_at", "revoked_at", "max_ttl_seconds")
    search_fields = ("user__username",)
    raw_id_fields = ("user", "granted_by")
    readonly_fields = ("granted_at",)


@admin.register(SurveyTokenRequest)
class SurveyTokenRequestAdmin(ModelAdmin):
    list_display = ("requester", "status", "submitted_at", "reviewed_by", "reviewed_at")
    list_filter = ("status",)
    search_fields = ("requester__username", "justification")
    raw_id_fields = ("requester", "reviewed_by")
    readonly_fields = ("submitted_at",)


@admin.register(SurveySegment)
class SurveySegmentAdmin(ModelAdmin):
    """Named cohorts the statistics page can be recomputed inside.

    Editable, unlike the rest of this module: a segment is an editorial decision about
    which questions the page can answer, not survey data. ``criteria`` is validated
    against an allowlist of published facets when saved, so a clause that the rollup
    could not apply is refused here rather than failing silently at midnight.

    Changing a segment does not restate the numbers until the next rollup, which is
    nightly. Run ``manage.py survey_rollup`` to see it immediately.
    """

    list_display = ("name", "slug", "enabled", "position", "created_at")
    list_filter = ("enabled",)
    search_fields = ("name", "slug", "description")
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ("created_at",)
