"""Admin registration for the vendors app."""
from __future__ import annotations

from django.contrib import admin
from unfold.admin import ModelAdmin, TabularInline

from lumina.vendors.models import Vendor, VendorAlias, VendorMembership, VendorProposal


class VendorMembershipInline(TabularInline):
    model = VendorMembership
    extra = 0
    autocomplete_fields = ("user",)


@admin.register(Vendor)
class VendorAdmin(ModelAdmin):
    list_display = ("name", "slug", "verified", "homepage")
    list_filter = ("verified",)
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}
    inlines = [VendorMembershipInline]


@admin.register(VendorMembership)
class VendorMembershipAdmin(ModelAdmin):
    list_display = ("user", "vendor", "role", "created_at")
    list_filter = ("role", "vendor")
    autocomplete_fields = ("user", "vendor")


@admin.register(VendorProposal)
class VendorProposalAdmin(ModelAdmin):
    list_display = ("kind", "name", "target", "proposed_by", "status", "submitted_at")
    list_filter = ("kind", "status")
    search_fields = ("name", "target__name", "proposed_by__username")
    autocomplete_fields = ("target", "proposed_by", "reviewed_by")
    readonly_fields = ("submitted_at", "reviewed_at")


@admin.register(VendorAlias)
class VendorAliasAdmin(ModelAdmin):
    list_display = ("name", "vendor")
    search_fields = ("name", "vendor__name")
    autocomplete_fields = ("vendor",)
