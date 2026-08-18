"""Reviewer dashboard URLs."""
from __future__ import annotations

from django.urls import path

from lumina.review import run_views, views

app_name = "review"

urlpatterns = [
    path("", views.queue, name="queue"),
    path("archive/", views.archive, name="archive"),
    path("runs/<int:pk>/", run_views.run_detail, name="run_detail"),
    path("runs/<int:pk>/assign/", run_views.run_assign_listing, name="run_assign_listing"),
    path("runs/<int:pk>/components/", run_views.run_component_ties,
         name="run_component_ties"),
    path("runs/<int:pk>/approve/", run_views.run_approve, name="run_approve"),
    path("runs/<int:pk>/approve-group/", run_views.run_approve_group,
         name="run_approve_group"),
    path("runs/<int:pk>/reject/", run_views.run_reject, name="run_reject"),
    path("runs/<int:pk>/release-quarantine/", run_views.run_release_quarantine,
         name="run_release_quarantine"),
    path(
        "runs/<int:pk>/request-changes/",
        run_views.run_request_changes,
        name="run_request_changes",
    ),
    path("<int:pk>/", views.detail, name="detail"),
    path("<int:pk>/tweak/", views.tweak, name="tweak"),
    path("<int:pk>/approve/", views.approve, name="approve"),
    path("<int:pk>/reject/", views.reject, name="reject"),
    path("<int:pk>/request-changes/", views.request_changes, name="request_changes"),
    path("values/<int:pk>/promote/", views.promote_value, name="promote_value"),
    path("values/<int:pk>/reject/", views.reject_value, name="reject_value"),
    path("vendors/<int:pk>/", views.vendor_proposal_detail, name="vendor_proposal_detail"),
    path("vendors/<int:pk>/approve/", views.vendor_proposal_approve, name="vendor_proposal_approve"),
    path("vendors/<int:pk>/reject/", views.vendor_proposal_reject, name="vendor_proposal_reject"),
    path("software/<int:pk>/approve/", views.software_approve, name="software_approve"),
    path("software/<int:pk>/reject/", views.software_reject, name="software_reject"),
    path("software/<int:pk>/request-changes/", views.software_request_changes,
         name="software_request_changes"),
    path("software-majors/<int:pk>/approve/", views.software_major_approve,
         name="software_major_approve"),
    path("software-majors/<int:pk>/reject/", views.software_major_reject,
         name="software_major_reject"),
    path("software-edits/<int:pk>/approve/", views.software_edit_approve,
         name="software_edit_approve"),
    path("software-edits/<int:pk>/reject/", views.software_edit_reject,
         name="software_edit_reject"),
    path("claims/<int:pk>/approve/", views.vendor_claim_approve, name="vendor_claim_approve"),
    path("claims/<int:pk>/reject/", views.vendor_claim_reject, name="vendor_claim_reject"),
    path("claims/<int:pk>/request-changes/", views.vendor_claim_request_changes,
         name="vendor_claim_request_changes"),
    path("listings/<int:pk>/", views.listing_edit_detail, name="listing_edit_detail"),
    path("listings/<int:pk>/approve/", views.listing_edit_approve, name="listing_edit_approve"),
    path("listings/<int:pk>/reject/", views.listing_edit_reject, name="listing_edit_reject"),
]
