"""Submission flow URLs."""
from __future__ import annotations

from django.urls import path

from lumina.hardware import submit_views

urlpatterns = [
    path("", submit_views.start, name="start"),
    # Keyed on the submission's uuid, not its pk, matching ``software:revise``: the
    # link is emailed and pasted around, and a sequential id in it invites walking.
    path("revise/<uuid:uuid>/", submit_views.revise, name="revise"),
    # Evidence files. Keyed on pk rather than a uuid because the view authorizes rather
    # than relying on the URL being hard to guess - which is the whole point of moving
    # these off the public /media/ alias.
    path("attachments/<int:pk>/", submit_views.attachment, name="attachment"),
]
