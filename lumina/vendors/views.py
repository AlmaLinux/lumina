"""Vendor profile proposal views.

Public surface (under ``/vendors/...``):

- ``GET/POST /vendors/propose-new/``  → propose a brand-new Vendor.
  Auth required, no other permission.
- ``GET/POST /vendors/<slug>/propose-edit/`` → propose an edit to an
  existing Vendor. Restricted by ``can_propose_vendor_edit``.

Both views write a pending VendorProposal and redirect to the user
dashboard with a flash message. Reviewer actions live in lumina.review.
"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, HttpResponseForbidden, HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import reverse

from lumina.vendors.forms import (
    VendorClaimForm,
    VendorCreateProposalForm,
    VendorEditProposalForm,
)
from lumina.vendors.models import Vendor, VendorProposal
from lumina.vendors.services import can_propose_vendor_edit, claim_vendor


@login_required
def propose_new(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        form = VendorCreateProposalForm(request.POST, request.FILES)
        if form.is_valid():
            proposal: VendorProposal = form.save(commit=False)
            proposal.kind = VendorProposal.KIND_CREATE
            proposal.proposed_by = request.user
            proposal.save()
            messages.success(request, "Vendor proposal submitted for review.")
            return HttpResponseRedirect(reverse("accounts:dashboard"))
    else:
        form = VendorCreateProposalForm()
    return render(request, "vendors/propose_new.html", {"form": form})


@login_required
def propose_edit(request: HttpRequest, slug: str) -> HttpResponse:
    vendor = get_object_or_404(Vendor, slug=slug)
    if not can_propose_vendor_edit(request.user, vendor):
        return HttpResponseForbidden(
            "You need a submit-role membership in this vendor to propose edits."
        )
    if request.method == "POST":
        form = VendorEditProposalForm(request.POST, request.FILES, vendor=vendor)
        if form.is_valid():
            proposal: VendorProposal = form.save(commit=False)
            proposal.kind = VendorProposal.KIND_UPDATE
            proposal.target = vendor
            proposal.proposed_by = request.user
            proposal.save()
            messages.success(request, f"Edit proposal for {vendor.name} submitted for review.")
            return HttpResponseRedirect(reverse("accounts:dashboard"))
    else:
        form = VendorEditProposalForm(vendor=vendor)
    return render(request, "vendors/propose_edit.html", {"form": form, "vendor": vendor})


@login_required
def claim(request: HttpRequest, slug: str) -> HttpResponse:
    """Open a claim on a vendor record.

    Reached from a listing rather than a vendor page, because lumina has no
    public vendor pages - the entry point is "are you the vendor?" next to a
    listing someone else created on their behalf.
    """
    vendor = get_object_or_404(Vendor, slug=slug)
    form = VendorClaimForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            claim_vendor(
                vendor=vendor,
                requester=request.user,
                work_email=form.cleaned_data["work_email"],
                role_at_vendor=form.cleaned_data["role_at_vendor"],
                note=form.cleaned_data["note"],
                evidence=form.cleaned_data.get("evidence"),
            )
        except ValueError as exc:
            # An open claim already exists. That is a state to report, not an
            # error page.
            messages.info(request, str(exc))
        else:
            messages.success(
                request,
                f"Claim for {vendor.name} submitted. A reviewer will look at it.",
            )
        return HttpResponseRedirect(reverse("accounts:dashboard"))
    return render(
        request,
        "vendors/claim.html",
        {"form": form, "vendor": vendor, "is_claimed": vendor.is_claimed},
    )
