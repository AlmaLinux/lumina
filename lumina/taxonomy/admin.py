"""Admin registration for the taxonomy app.

Categories and their values are admin-managed here. Pending-value
promotion/rejection is handled in the in-app review UI (``/review/``) so
admins don't need to log into the Django admin for day-to-day workflow -
but the admin still exposes the records for data fixes.
"""
from __future__ import annotations

from django.contrib import admin
from unfold.admin import ModelAdmin, TabularInline

from lumina.taxonomy.models import Category, CategoryValue


class CategoryValueInline(TabularInline):
    model = CategoryValue
    extra = 0
    fields = ("value", "slug", "status", "proposed_by", "approved_by", "approved_at")
    readonly_fields = ("approved_at",)


@admin.register(Category)
class CategoryAdmin(ModelAdmin):
    list_display = ("name", "slug", "applies_to", "picker_widget", "allow_suggestions", "collapsed_limit", "display_order")
    list_editable = ("applies_to", "picker_widget", "allow_suggestions", "collapsed_limit", "display_order")
    prepopulated_fields = {"slug": ("name",)}
    search_fields = ("name", "slug")
    inlines = [CategoryValueInline]


@admin.register(CategoryValue)
class CategoryValueAdmin(ModelAdmin):
    list_display = ("category", "value", "status", "proposed_by", "approved_by", "created_at")
    list_filter = ("status", "category")
    search_fields = ("value", "category__name")
    autocomplete_fields = ("category", "proposed_by", "approved_by")
