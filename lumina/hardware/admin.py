"""Admin registration for hardware models.

Day-to-day submission review happens at ``/review/``, not here. The admin
is exposed for data fixes: for example, correcting a wrong vendor on a
listing, or unpublishing something that shouldn't have gone live.
"""
from __future__ import annotations

import re

from django import forms
from django.contrib import admin
from django.core.exceptions import ValidationError
from unfold.admin import ModelAdmin, TabularInline

from lumina.hardware.models import (
    CommunityAttestation,
    Component,
    ComponentExclusionRule,
    ComponentRole,
    ListingCategoryValue,
    ListingEditProposal,
    ListingVersion,
    Submission,
    System,
    TestResultAttachment,
)


class ListingCategoryValueInline(TabularInline):
    model = ListingCategoryValue
    extra = 0
    autocomplete_fields = ("value",)


class ListingVersionInline(TabularInline):
    """AlmaLinux compatibility, editable per listing.

    There was no way to set this at all: rows only appeared when
    ``record_compatibility`` wrote one off the back of a passing run, so a
    curated family showed exactly the releases someone happened to have tested
    on. An AMD Ryzen 7000 that has only ever been validated on 10.2 read as
    "AlmaLinux 10.2+" when it plainly runs on 9 as well.

    ``source`` distinguishes the two: rows a run proved are marked as such and
    are not the same claim as rows added here by hand. Editing a proven row's
    minor is allowed - a reviewer may know better than one run - but a run
    passing on an earlier minor will lower it again, because evidence wins.
    """

    model = ListingVersion
    extra = 0
    fields = ("release", "source")
    autocomplete_fields = ("release",)


class _ListingAdminBase(ModelAdmin):
    list_display = ("name", "vendor", "owner_vendor", "model_number", "validation_level", "published", "attestation_count")
    list_filter = ("validation_level", "published", "vendor", "owner_vendor")
    search_fields = ("name", "model_number", "slug")
    autocomplete_fields = ("vendor", "owner_vendor", "created_by")
    readonly_fields = ("attestation_count",)
    prepopulated_fields = {"slug": ("name",)}


@admin.register(System)
class SystemAdmin(_ListingAdminBase):
    # Systems get the ListingCategoryValue inline via a custom form because
    # the M2M through-model uses listing_system, not listing_component.
    inlines = [ListingCategoryValueInline, ListingVersionInline]
    # supported_cpus is the one a human maintains, from the vendor's spec
    # sheet; cpus is written by approved runs. Both are shown because a
    # reviewer occasionally needs to correct the automatic one.
    filter_horizontal = ("supported_cpus", "cpus", "related_components")

    def get_inline_instances(self, request, obj=None):
        # Both inlines need fk_name pinned: their models carry a FK to each
        # listing kind and Django cannot tell which one this admin means.
        instances = []
        for inline_cls in self.inlines:
            inline = inline_cls(self.model, self.admin_site)
            inline.fk_name = "listing_system"
            instances.append(inline)
        return instances


class ModelPatternsWidget(forms.Textarea):
    """Edit the JSON pattern list as one regex per line."""

    def format_value(self, value):
        if isinstance(value, str):
            try:
                import json

                value = json.loads(value)
            except (ValueError, TypeError):
                return value
        return "\n".join(value or [])


class ModelPatternsField(forms.CharField):
    widget = ModelPatternsWidget

    def clean(self, value):
        lines = [line.strip() for line in (value or "").splitlines() if line.strip()]
        for line in lines:
            try:
                re.compile(line)
            except re.error as exc:
                raise ValidationError(f"{line!r} is not a valid regex: {exc}") from exc
        return lines


class ComponentAdminForm(forms.ModelForm):
    """Keeps role and patterns consistent: patterns only mean something on a
    family, and a family without them matches nothing."""

    model_patterns = ModelPatternsField(
        required=False,
        help_text=(
            "One regular expression per line. A component with patterns is a "
            "family: matching model strings roll up to it for certification, "
            "while benchmarks stay per-model. Example for AMD EPYC 9004: "
            r"EPYC 9[0-9]{2}4\b"
        ),
        widget=ModelPatternsWidget(attrs={"rows": 4, "class": "vLargeTextField"}),
    )

    class Meta:
        model = Component
        # DJ007: name the fields rather than "__all__", so a new model field
        # never silently appears in the admin form.
        fields = (
            "name", "vendor", "owner_vendor", "kind", "role", "model_number",
            "description", "vendor_spec_url", "slug", "published",
            "validation_level", "attestation_count", "attributes",
            "model_patterns",
        )

    def clean(self):
        cleaned = super().clean()
        role = cleaned.get("role")
        patterns = cleaned.get("model_patterns") or []
        if patterns and role != ComponentRole.FAMILY:
            self.add_error(
                "model_patterns",
                "Only a family can carry matching patterns. Set the role to "
                "“Family / generation”, or clear the patterns.",
            )
        if role == ComponentRole.FAMILY and not patterns:
            self.add_error(
                "model_patterns",
                "A family needs at least one pattern, otherwise no reported "
                "model will roll up to it.",
            )
        return cleaned


@admin.register(Component)
class ComponentAdmin(_ListingAdminBase):
    form = ComponentAdminForm
    inlines = [ListingCategoryValueInline, ListingVersionInline]
    # Whether a component is a family is the most consequential thing about
    # it (certification rolls up to families), so surface it in the list.
    list_display = (
        "name", "vendor", "kind", "role", "pattern_summary", "rollup_summary",
        "validation_level", "published",
    )
    list_filter = ("kind", "role", "validation_level", "published", "vendor")
    readonly_fields = ("attestation_count", "rollup_detail")
    fieldsets = (
        (None, {
            "fields": ("name", "vendor", "owner_vendor", "kind",
                       "model_number", "slug"),
        }),
        ("Family matching", {
            "description": (
                "Only <strong>families</strong> use this section. Reported "
                "model strings matching any pattern roll up to the family, so "
                "certification is granted once per generation instead of per "
                "SKU; benchmarks always stay per-model. Patterns are applied "
                "when pages are rendered, so a change here reclassifies "
                "existing results immediately - no re-import needed."
            ),
            # role belongs beside the patterns because they are two halves of
            # one decision, and clean() rejects a mismatch between them. Left
            # out of the fieldsets it was never rendered, so every save of a
            # family posted no role and failed its own validation - the family
            # patterns were uneditable through the admin entirely.
            "fields": ("role", "model_patterns", "rollup_detail"),
        }),
        ("Catalog listing", {
            "fields": ("description", "vendor_spec_url", "published",
                       "validation_level", "attestation_count"),
        }),
        ("Advanced", {
            "classes": ("collapse",),
            "description": (
                "Kind-specific detail captured from certification runs, e.g. "
                "a GPU's driver version or a drive's media type."
            ),
            "fields": ("attributes",),
        }),
    )

    @admin.display(description="Patterns")
    def pattern_summary(self, obj) -> str:
        patterns = obj.model_patterns or []
        if not patterns:
            return "-"
        head = ", ".join(patterns[:2])
        return head + (f" (+{len(patterns) - 2})" if len(patterns) > 2 else "")

    @admin.display(description="Rolls up")
    def rollup_summary(self, obj) -> str:
        """Make the family/model relationship visible in the list, so the
        translation is inspectable rather than something that just happens."""
        if obj.is_family:
            count = obj.matching_models().count()
            return f"{count} model{'' if count == 1 else 's'}"
        family = obj.resolved_family()
        return f"→ {family.name}" if family else "-"

    @admin.display(description="Currently matches")
    def rollup_detail(self, obj) -> str:
        if not obj.pk or not obj.is_family:
            return "Set the role to a family to match reported models."
        names = list(obj.matching_models().values_list("name", flat=True))
        if not names:
            return (
                "No model entries match these patterns yet. Reported models "
                "still roll up as results arrive."
            )
        return ", ".join(names)

    def get_inline_instances(self, request, obj=None):
        # Driven off self.inlines rather than naming one class, so adding an
        # inline to the list is enough - this override existed only to pin
        # fk_name, and silently dropped anything it did not mention.
        instances = []
        for inline_cls in self.inlines:
            inline = inline_cls(self.model, self.admin_site)
            inline.fk_name = "listing_component"
            instances.append(inline)
        return instances


class TestResultAttachmentInline(TabularInline):
    model = TestResultAttachment
    extra = 0
    readonly_fields = ("sha256", "uploaded_at")


@admin.register(Submission)
class SubmissionAdmin(ModelAdmin):
    list_display = ("uuid", "submitter", "on_behalf_of", "claimed_validation_level", "status", "submitted_at")
    list_filter = ("status", "claimed_validation_level")
    search_fields = ("uuid", "submitter__username")
    autocomplete_fields = ("submitter", "on_behalf_of", "listing_system", "listing_component", "reviewed_by")
    readonly_fields = ("uuid", "submitted_at", "reviewed_at")
    inlines = [TestResultAttachmentInline]


@admin.register(CommunityAttestation)
class CommunityAttestationAdmin(ModelAdmin):
    list_display = ("submission", "listing_system", "listing_component", "created_at")
    readonly_fields = ("submission", "created_at")


@admin.register(ListingEditProposal)
class ListingEditProposalAdmin(ModelAdmin):
    list_display = ("__str__", "proposed_by", "status", "submitted_at", "reviewed_at")
    list_filter = ("status",)
    search_fields = ("name", "model_number", "proposed_by__username")
    autocomplete_fields = ("listing_system", "listing_component", "proposed_by", "reviewed_by")
    readonly_fields = ("submitted_at", "reviewed_at")


@admin.register(ComponentExclusionRule)
class ComponentExclusionRuleAdmin(ModelAdmin):
    """The curated list of PCI devices the catalog should not auto-attach.

    Add a row to stop a specific part (an onboard iGPU, a management NIC) from being ticked by
    default on the review screen; it is still kept in the run's inventory and a reviewer can include
    it. BMC display adapters are excluded categorically in code and need no row here.
    """

    list_display = ("__str__", "vendor_id", "device_id", "kind", "reason", "enabled")
    list_filter = ("enabled", "kind")
    search_fields = ("vendor_id", "device_id", "reason")
