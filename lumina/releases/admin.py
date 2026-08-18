from __future__ import annotations

from django.contrib import admin
from unfold.admin import ModelAdmin

from lumina.releases.models import AlmaLinuxRelease


@admin.register(AlmaLinuxRelease)
class AlmaLinuxReleaseAdmin(ModelAdmin):
    list_display = ("major", "supported", "latest_minor", "created_at")
    # Editable in the list, because bumping the shipped minor is a data change made the day a
    # minor lands - not a deploy, and not something to hard-code. Raising it is what lifts the
    # "enablement lands in 10.3" disclaimer off every listing waiting on that minor, so this is
    # the one control an administrator has over that whole class of entries.
    list_editable = ("supported", "latest_minor")
    list_filter = ("supported",)
    search_fields = ("major",)
