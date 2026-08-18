"""Reviewer dashboard + submission-action views.

The review UI lives here (not in the Django admin) so reviewers don't need
staff status and so the UX can be tuned to the review workflow. Admins can
still reach the admin site for system-configuration tasks (categories,
vendors, users).
"""
from __future__ import annotations

from django.contrib import messages
from django.db.models import Count
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from lumina.audit.services import log_action
from lumina.core.certification import ValidationLevel
from lumina.hardware.forms import ReviewerListingEditForm
from lumina.hardware.models import ListingEditProposal, Submission
from lumina.hardware.services import annotate_similar_listings, similar_listings
from lumina.notifications.services import emit
from lumina.review.permissions import reviewer_required
from lumina.software import services as software_services
from lumina.software.models import (
    SoftwareCompatibility,
    SoftwareEditProposal,
    SoftwareSubmission,
)
from lumina.taxonomy.models import CategoryValue
from lumina.vendors.models import VendorClaim, VendorProposal

# Every reviewable model now inherits OPEN_STATUSES from ReviewWorkflow. Three of
# the four used to lack it, which is why this file re-derived the same tuple by hand
# three times - and why a change to what "open" means would have had to be made here
# as well as in the models.
_OPEN_STATUSES = Submission.OPEN_STATUSES
_OPEN_PROPOSAL_STATUSES = VendorProposal.OPEN_STATUSES
_OPEN_CLAIM_STATUSES = VendorClaim.OPEN_STATUSES
_OPEN_EDIT_STATUSES = ListingEditProposal.OPEN_STATUSES


@reviewer_required
def queue(request: HttpRequest) -> HttpResponse:
    from lumina.results.models import RunType, TestRun

    submissions = list(
        Submission.objects.filter(status__in=_OPEN_STATUSES)
        .select_related(
            "submitter", "on_behalf_of",
            "listing_system", "listing_system__vendor",
            "listing_component", "listing_component__vendor",
        )
        # How much evidence is attached, so a reviewer can see which submissions are
        # bare assertions before opening them. Annotated rather than counted in the
        # template, which would be a query per row.
        .annotate(attachment_count=Count("attachments", distinct=True))
        .prefetch_related("cited_releases")
        .order_by("submitted_at")
    )
    # A declared submission always creates a new listing, so two people cataloguing
    # one machine fork it silently. Flagged in the queue as well as on the detail
    # page because a reviewer working the queue approves from here.
    annotate_similar_listings(submissions)
    vendor_proposals = (
        VendorProposal.objects.filter(status__in=_OPEN_PROPOSAL_STATUSES)
        .select_related("proposed_by", "target")
        .order_by("submitted_at")
    )
    listing_edits = (
        ListingEditProposal.objects.filter(status__in=_OPEN_EDIT_STATUSES)
        .select_related("proposed_by", "listing_system", "listing_component")
        .order_by("submitted_at")
    )
    # Claims share the vendors pane rather than getting a tab of their own: both
    # are decisions about a vendor record, and the queue already has five tabs.
    vendor_claims = (
        VendorClaim.objects.filter(status__in=_OPEN_CLAIM_STATUSES)
        .select_related("requester", "vendor")
        .order_by("submitted_at")
    )
    from lumina.survey.models import SurveySubmission, SurveyTokenRequest
    survey_token_requests = (
        SurveyTokenRequest.objects.filter(status__in=SurveyTokenRequest.OPEN_STATUSES)
        .select_related("requester")
        .order_by("submitted_at")
    )
    # The survey moderation queue: unreviewed submissions, newest first. Capped, because
    # a census can produce many - review is oversight, not a gate, so a reviewer
    # spot-checks and dismisses anomalies rather than clearing every row.
    survey_submissions = (
        SurveySubmission.objects.pending_review()
        .select_related("submitter", "token")
        .order_by("-received_at")[:100]
    )
    # What the pane actually lists: recent submissions whatever their origin or review
    # state. The census is a standing stream and this is its only in-app view, so a
    # reviewer can see it is flowing - and spot-check a cert-run fork, which is counted
    # but never queued - on a day when nothing is waiting. ``survey_submissions`` above
    # stays the queue proper, and is what the tab badge counts.
    survey_recent = (
        SurveySubmission.objects.select_related("submitter", "token", "reviewed_by")
        .order_by("-received_at")[:100]
    )
    # One software tab holding three tables rather than three more tabs: the
    # queue is already five deep, and all three are software decisions.
    software_submissions = (
        SoftwareSubmission.objects.filter(status__in=SoftwareSubmission.OPEN_STATUSES)
        .select_related("submitter", "on_behalf_of", "software", "software__vendor")
        .order_by("submitted_at")
    )
    software_edits = (
        SoftwareEditProposal.objects.filter(
            status__in=SoftwareEditProposal.OPEN_STATUSES
        )
        .select_related("proposed_by", "software")
        .order_by("submitted_at")
    )
    reported_majors = (
        SoftwareCompatibility.objects.pending()
        .select_related("software", "release", "proposed_by")
        .order_by("software__name", "-release__major")
    )
    # Benchmark runs are reviewed separately from validation runs:
    # one queue is certification evidence, the other feeds the leaderboards.
    run_base = TestRun.objects.open_for_review().select_related(
        "submitter", "alma_release", "listing_system"
    ).order_by("received_at")
    # The validation tab shows each run's PASS/FAIL verdict, which calls verdict() per row;
    # prefetch results so a validate row answers from memory instead of an EXISTS query each. The
    # benchmark tab shows no verdict (benchmark runs have none), so it needs no prefetch. A
    # collect-only run cannot reach here at all now (ingest refuses it as a survey), so excluding
    # benchmarks is enough to leave just validate runs.
    validation_runs = run_base.exclude(
        run_type=RunType.benchmark.value
    ).prefetch_related("results")
    benchmark_runs = run_base.filter(run_type=RunType.benchmark.value)
    # Its own pane. The question a reviewer answers here is not "is this hardware
    # certified" but "is this report wrong about its operating system", so putting
    # these in with the rest would be two different jobs in one list.
    quarantined_runs = (
        TestRun.objects.quarantined()
        .select_related("submitter", "listing_system")
        .order_by("received_at")
    )
    return render(
        request,
        "review/queue.html",
        {
            "submissions": submissions,
            "vendor_proposals": vendor_proposals,
            "vendor_claims": vendor_claims,
            # One badge for the pane, because a template cannot add two lengths.
            "vendor_queue_count": len(vendor_proposals) + len(vendor_claims),
            "listing_edits": listing_edits,
            "software_submissions": software_submissions,
            "software_edits": software_edits,
            "reported_majors": reported_majors,
            "software_queue_count": (
                len(software_submissions) + len(software_edits) + len(reported_majors)
            ),
            "validation_runs": validation_runs,
            "benchmark_runs": benchmark_runs,
            "quarantined_runs": quarantined_runs,
            "survey_token_requests": survey_token_requests,
            "survey_submissions": survey_submissions,
            "survey_recent": survey_recent,
        },
    )


@reviewer_required
def archive(request: HttpRequest) -> HttpResponse:
    """Already-decided work, plus the raw activity log.

    The queue shows only open items, so a decision disappeared the moment it was made and
    no page a reviewer could reach recorded who made it. Reviewers are deliberately not
    staff, so ``/admin/audit/`` - which has held all of this from the start - is exactly
    the place they cannot go.

    Two panes: decisions per reviewed object, and ``AuditLogEntry`` for actions that are
    not decisions about a reviewable object at all. See ``lumina.review.archive``.
    """
    from django.core.paginator import Paginator

    from lumina.audit.models import AuditLogEntry
    from lumina.review import archive as archive_service

    rows = archive_service.decisions()
    kind = request.GET.get("kind", "")
    if kind:
        rows = [r for r in rows if r["kind"] == kind]

    entries = (
        AuditLogEntry.objects.select_related("actor", "target_content_type")
        .order_by("-created_at")
    )
    action = request.GET.get("action", "")
    if action:
        entries = entries.filter(action=action)
    actor = request.GET.get("actor", "")
    if actor:
        entries = entries.filter(actor__username__icontains=actor)

    page = Paginator(entries, 50).get_page(request.GET.get("page"))
    return render(
        request,
        "review/archive.html",
        {
            "rows": rows,
            "kinds": archive_service.decision_kinds(),
            "selected_kind": kind,
            "page_obj": page,
            # Only the actions that actually occur, so the filter cannot offer a value
            # that returns nothing.
            "actions": list(
                AuditLogEntry.objects.order_by("action")
                .values_list("action", flat=True).distinct()
            ),
            "selected_action": action,
            "actor_query": actor,
        },
    )


@reviewer_required
def detail(request: HttpRequest, pk: int) -> HttpResponse:
    submission = get_object_or_404(Submission, pk=pk)
    pending_values = CategoryValue.objects.pending().filter(
        proposed_by=submission.submitter
    )
    edit_form = ReviewerListingEditForm(listing=submission.listing)
    return render(
        request,
        "review/detail.html",
        {
            "submission": submission,
            "pending_values": pending_values,
            "edit_form": edit_form,
            # Nothing prevents two declarations of one machine, so the reviewer is
            # the only place a fork gets caught. See hardware.services.
            "similar_listings": similar_listings(submission.listing),
            # The releases this submission actually claims. Approval acts on these
            # (``Submission.cited_releases``) and the page never showed them, so a
            # reviewer was approving a compatibility claim they could not read.
            "cited_releases": list(
                submission.cited_releases.order_by("-major")
            ),
            # What the reviewer may actually award. One tier, because a manual
            # submission is declared evidence; see Submission.MANUAL_CEILING.
            "final_levels": [
                (
                    Submission.MANUAL_CEILING,
                    ValidationLevel(Submission.MANUAL_CEILING).label,
                )
            ],
        },
    )


@reviewer_required
@require_POST
def tweak(request: HttpRequest, pk: int) -> HttpResponse:
    """Apply reviewer-side edits to the submission's listing (and any inline
    vendor / CPUs) without approving yet. Reviewer can iterate, then hit
    Approve when satisfied."""
    submission = get_object_or_404(Submission, pk=pk)
    form = ReviewerListingEditForm(request.POST, listing=submission.listing)
    if form.is_valid():
        form.save()
        log_action("submission.tweak", target=submission, after=form.cleaned_data)
        messages.success(request, "Submission updated.")
    else:
        messages.error(request, "Could not save changes - see field errors.")
    return HttpResponseRedirect(reverse("review:detail", args=[submission.pk]))


@reviewer_required
@require_POST
def approve(request: HttpRequest, pk: int) -> HttpResponse:
    submission = get_object_or_404(Submission, pk=pk)
    final_level = request.POST.get("final_level") or submission.claimed_validation_level
    # Validate the posted level against the choices so we don't pass garbage
    # into the state machine. ValidationLevel is a TextChoices so ``values``
    # is the list of accepted strings.
    if final_level not in ValidationLevel.values:
        return HttpResponse("Invalid final_level", status=400)
    # Enum membership is the only check *here*, and deliberately so - but it used to be
    # the only check anywhere, which is not a permission check at all. A submitter with
    # no vendor membership and no staff flag came out Vendor-validated, the top tier in
    # the system, because the reviewer's POST said so; an empty POST body did it too, by
    # falling back to the submitter's own claim. The cap now lives on the model
    # (``Submission.MANUAL_CEILING``, applied in ``approve``) so that it holds for every
    # caller rather than for this one view, and is not re-derived here: two overlapping
    # rules invite drifting apart, and a check in a view is a check one shell script
    # away from being bypassed.

    before = {"status": submission.status, "validation_level": submission.listing.validation_level}
    submission.approve(by=request.user, final_level=final_level)
    after = {"status": submission.status, "validation_level": submission.listing.validation_level}
    log_action("submission.approve", target=submission, before=before, after=after)
    emit("submission.approved", target=submission, actor=request.user)
    return HttpResponseRedirect(reverse("review:queue"))


@reviewer_required
@require_POST
def reject(request: HttpRequest, pk: int) -> HttpResponse:
    submission = get_object_or_404(Submission, pk=pk)
    reason = request.POST.get("reason", "")
    before = {"status": submission.status}
    submission.reject(by=request.user, reason=reason)
    log_action(
        "submission.reject",
        target=submission,
        before=before,
        after={"status": submission.status},
        notes=reason,
    )
    emit("submission.rejected", target=submission, actor=request.user)
    return HttpResponseRedirect(reverse("review:queue"))


@reviewer_required
@require_POST
def request_changes(request: HttpRequest, pk: int) -> HttpResponse:
    submission = get_object_or_404(Submission, pk=pk)
    reason = request.POST.get("reason", "")
    submission.request_changes(by=request.user, reason=reason)
    log_action("submission.request_changes", target=submission, notes=reason)
    emit("submission.needs_changes", target=submission, actor=request.user)
    return HttpResponseRedirect(reverse("review:queue"))


@reviewer_required
@require_POST
def promote_value(request: HttpRequest, pk: int) -> HttpResponse:
    value = get_object_or_404(CategoryValue, pk=pk)
    value.approve(by=request.user)
    log_action("taxonomy.value.approve", target=value)
    return HttpResponseRedirect(reverse("review:queue"))


@reviewer_required
@require_POST
def reject_value(request: HttpRequest, pk: int) -> HttpResponse:
    value = get_object_or_404(CategoryValue, pk=pk)
    value.reject(by=request.user)
    log_action("taxonomy.value.reject", target=value)
    return HttpResponseRedirect(reverse("review:queue"))


# -- Vendor proposal review actions ----------------------------------------

@reviewer_required
def vendor_proposal_detail(request: HttpRequest, pk: int) -> HttpResponse:
    proposal = get_object_or_404(VendorProposal, pk=pk)
    return render(request, "review/vendor_proposal_detail.html", {"proposal": proposal})


@reviewer_required
@require_POST
def vendor_proposal_approve(request: HttpRequest, pk: int) -> HttpResponse:
    proposal = get_object_or_404(VendorProposal, pk=pk)
    before = {"status": proposal.status, "target_id": proposal.target_id}
    proposal.approve(by=request.user)
    log_action(
        "vendor_proposal.approve", target=proposal,
        before=before,
        after={"status": proposal.status, "target_id": proposal.target_id},
    )
    return HttpResponseRedirect(reverse("review:queue"))


@reviewer_required
@require_POST
def vendor_proposal_reject(request: HttpRequest, pk: int) -> HttpResponse:
    proposal = get_object_or_404(VendorProposal, pk=pk)
    reason = request.POST.get("reason", "")
    proposal.reject(by=request.user, reason=reason)
    log_action("vendor_proposal.reject", target=proposal, notes=reason)
    return HttpResponseRedirect(reverse("review:queue"))


# -- Listing edit proposal review actions ------------------------------------

@reviewer_required
def listing_edit_detail(request: HttpRequest, pk: int) -> HttpResponse:
    proposal = get_object_or_404(ListingEditProposal, pk=pk)
    return render(
        request, "review/listing_edit_detail.html", {"proposal": proposal},
    )


@reviewer_required
@require_POST
def listing_edit_approve(request: HttpRequest, pk: int) -> HttpResponse:
    proposal = get_object_or_404(ListingEditProposal, pk=pk)
    before = {f: getattr(proposal.target, f) for f in proposal._COPIED_FIELDS}
    proposal.approve(by=request.user)
    after = {f: getattr(proposal.target, f) for f in proposal._COPIED_FIELDS}
    log_action(
        "listing_edit.approve", target=proposal,
        before=before, after=after,
    )
    return HttpResponseRedirect(reverse("review:queue"))


@reviewer_required
@require_POST
def listing_edit_reject(request: HttpRequest, pk: int) -> HttpResponse:
    proposal = get_object_or_404(ListingEditProposal, pk=pk)
    reason = request.POST.get("reason", "")
    proposal.reject(by=request.user, reason=reason)
    log_action("listing_edit.reject", target=proposal, notes=reason)
    return HttpResponseRedirect(reverse("review:queue"))


@reviewer_required
@require_POST
def vendor_claim_approve(request: HttpRequest, pk: int) -> HttpResponse:
    """Recognise the claimant as representing the vendor.

    ``verify`` and ``demote_others`` are checkbox posts rather than always-on,
    because they are two separate judgements the reviewer makes while looking at
    the claim: whether this vendor may self-certify, and whether the people
    already holding submit rights on it are colleagues or squatters.
    """
    claim = get_object_or_404(VendorClaim, pk=pk)
    before = {"status": claim.status, "verified": claim.vendor.verified}
    try:
        moved = claim.approve(
            by=request.user,
            verify=bool(request.POST.get("verify")),
            # Default on: the common case is a stranger who typed the vendor's
            # name into a submit form and must not keep submit rights once the
            # vendor is verified.
            demote_others=request.POST.get("keep_members") != "on",
        )
    except ValueError as exc:
        # Hardware's submission endpoints let this become a 500 on a double
        # submit. Reporting it is the whole fix.
        messages.error(request, str(exc))
        return HttpResponseRedirect(reverse("review:queue"))
    claim.vendor.refresh_from_db()
    log_action(
        "vendor_claim.approve", target=claim, before=before,
        after={"status": claim.status, "verified": claim.vendor.verified,
               "listings_transferred": moved},
    )
    emit("vendor_claim.decided", target=claim, actor=request.user)
    transferred = ", ".join(f"{n} {kind}" for kind, n in moved.items() if n)
    messages.success(
        request,
        f"{claim.requester} now owns {claim.vendor.name}."
        + (f" Transferred {transferred}." if transferred else "")
        + ("" if claim.vendor.verified else
           " Not marked verified, so it cannot self-certify yet."),
    )
    return HttpResponseRedirect(reverse("review:queue"))


@reviewer_required
@require_POST
def vendor_claim_reject(request: HttpRequest, pk: int) -> HttpResponse:
    claim = get_object_or_404(VendorClaim, pk=pk)
    reason = request.POST.get("reason", "")
    before = {"status": claim.status}
    try:
        claim.reject(by=request.user, reason=reason)
    except ValueError as exc:
        messages.error(request, str(exc))
        return HttpResponseRedirect(reverse("review:queue"))
    log_action(
        "vendor_claim.reject", target=claim, before=before,
        after={"status": claim.status}, notes=reason,
    )
    emit("vendor_claim.decided", target=claim, actor=request.user)
    messages.info(request, f"Claim on {claim.vendor.name} rejected.")
    return HttpResponseRedirect(reverse("review:queue"))


@reviewer_required
@require_POST
def vendor_claim_request_changes(request: HttpRequest, pk: int) -> HttpResponse:
    claim = get_object_or_404(VendorClaim, pk=pk)
    reason = request.POST.get("reason", "")
    try:
        claim.request_changes(by=request.user, reason=reason)
    except ValueError as exc:
        messages.error(request, str(exc))
        return HttpResponseRedirect(reverse("review:queue"))
    log_action("vendor_claim.request_changes", target=claim, notes=reason)
    emit("vendor_claim.decided", target=claim, actor=request.user)
    messages.info(request, "Sent back to the claimant.")
    return HttpResponseRedirect(reverse("review:queue"))


@reviewer_required
@require_POST
def survey_token_approve(request: HttpRequest, pk: int) -> HttpResponse:
    """Grant an account the ability to mint long-lived survey tokens."""
    from lumina.survey.models import SurveyTokenRequest

    req = get_object_or_404(SurveyTokenRequest, pk=pk)
    before = {"status": req.status}
    try:
        req.approve(by=request.user)
    except ValueError as exc:
        messages.error(request, str(exc))
        return HttpResponseRedirect(reverse("review:queue"))
    log_action("survey_token_request.approve", target=req, before=before,
               after={"status": req.status})
    emit("survey_token_request.decided", target=req, actor=request.user)
    messages.success(request, f"{req.requester} can now mint long-lived survey tokens.")
    return HttpResponseRedirect(reverse("review:queue"))


@reviewer_required
@require_POST
def survey_token_reject(request: HttpRequest, pk: int) -> HttpResponse:
    from lumina.survey.models import SurveyTokenRequest

    req = get_object_or_404(SurveyTokenRequest, pk=pk)
    reason = request.POST.get("reason", "")
    try:
        req.reject(by=request.user, reason=reason)
    except ValueError as exc:
        messages.error(request, str(exc))
        return HttpResponseRedirect(reverse("review:queue"))
    log_action("survey_token_request.reject", target=req, notes=reason)
    emit("survey_token_request.decided", target=req, actor=request.user)
    messages.info(request, "Survey token request rejected.")
    return HttpResponseRedirect(reverse("review:queue"))


@reviewer_required
@require_POST
def survey_token_request_changes(request: HttpRequest, pk: int) -> HttpResponse:
    from lumina.survey.models import SurveyTokenRequest

    req = get_object_or_404(SurveyTokenRequest, pk=pk)
    reason = request.POST.get("reason", "")
    try:
        req.request_changes(by=request.user, reason=reason)
    except ValueError as exc:
        messages.error(request, str(exc))
        return HttpResponseRedirect(reverse("review:queue"))
    log_action("survey_token_request.request_changes", target=req, notes=reason)
    emit("survey_token_request.decided", target=req, actor=request.user)
    messages.info(request, "Sent back to the requester.")
    return HttpResponseRedirect(reverse("review:queue"))


@reviewer_required
@require_POST
def survey_submission_accept(request: HttpRequest, pk: int) -> HttpResponse:
    """Acknowledge a survey submission - reviewed, kept in stats."""
    from lumina.survey.models import SurveySubmission
    from lumina.survey.services import moderate_submission

    sub = get_object_or_404(SurveySubmission, pk=pk)
    moderate_submission(sub, by=request.user, dismiss=False)
    log_action("survey_submission.accept", target=sub, actor=request.user)
    messages.success(request, "Survey submission accepted.")
    return HttpResponseRedirect(reverse("review:queue"))


@reviewer_required
@require_POST
def survey_submission_dismiss(request: HttpRequest, pk: int) -> HttpResponse:
    """Exclude a survey submission from published stats (spam or anomaly)."""
    from lumina.survey.models import SurveySubmission
    from lumina.survey.services import moderate_submission

    sub = get_object_or_404(SurveySubmission, pk=pk)
    moderate_submission(sub, by=request.user, dismiss=True)
    log_action("survey_submission.dismiss", target=sub, actor=request.user)
    messages.info(request, "Survey submission dismissed (excluded from stats).")
    return HttpResponseRedirect(reverse("review:queue"))


@reviewer_required
@require_POST
def software_approve(request: HttpRequest, pk: int) -> HttpResponse:
    """Publish a software listing at the reviewer's chosen tier.

    The tier applies to every major the submission's listing cites - those were
    stored when the form was submitted, because the submission row carries no list
    of them.
    """
    submission = get_object_or_404(SoftwareSubmission, pk=pk)
    final_level = request.POST.get("final_level")
    if final_level not in ValidationLevel.values:
        return HttpResponse("Invalid final_level", status=400)
    before = {"status": submission.status,
              "validation_level": submission.software.validation_level}
    try:
        submission.approve(by=request.user, final_level=final_level)
    except ValueError as exc:
        messages.error(request, str(exc))
        return HttpResponseRedirect(reverse("review:queue"))
    submission.software.refresh_from_db()
    log_action(
        "software.approve", target=submission, before=before,
        after={"status": submission.status,
               "validation_level": submission.software.validation_level},
    )
    emit("submission.approved", target=submission, actor=request.user)
    messages.success(
        request, f"{submission.software.name} published."
    )
    return HttpResponseRedirect(reverse("review:queue"))


@reviewer_required
@require_POST
def software_reject(request: HttpRequest, pk: int) -> HttpResponse:
    submission = get_object_or_404(SoftwareSubmission, pk=pk)
    reason = request.POST.get("reason", "")
    before = {"status": submission.status}
    try:
        submission.reject(by=request.user, reason=reason)
    except ValueError as exc:
        messages.error(request, str(exc))
        return HttpResponseRedirect(reverse("review:queue"))
    log_action("software.reject", target=submission, before=before,
               after={"status": submission.status}, notes=reason)
    emit("submission.rejected", target=submission, actor=request.user)
    messages.info(request, "Software submission rejected.")
    return HttpResponseRedirect(reverse("review:queue"))


@reviewer_required
@require_POST
def software_request_changes(request: HttpRequest, pk: int) -> HttpResponse:
    submission = get_object_or_404(SoftwareSubmission, pk=pk)
    reason = request.POST.get("reason", "")
    try:
        submission.request_changes(by=request.user, reason=reason)
    except ValueError as exc:
        messages.error(request, str(exc))
        return HttpResponseRedirect(reverse("review:queue"))
    log_action("software.request_changes", target=submission, notes=reason)
    emit("submission.needs_changes", target=submission, actor=request.user)
    messages.info(request, "Sent back to the submitter.")
    return HttpResponseRedirect(reverse("review:queue"))


@reviewer_required
@require_POST
def software_major_approve(request: HttpRequest, pk: int) -> HttpResponse:
    """Accept a community-reported AlmaLinux major.

    The whole decision is "does this product work there", so there is no detail
    page - the queue row carries the product, the release, and who reported it.
    """
    row = get_object_or_404(SoftwareCompatibility, pk=pk)
    try:
        row.approve(by=request.user)
    except ValueError as exc:
        messages.error(request, str(exc))
        return HttpResponseRedirect(reverse("review:queue"))
    log_action(
        "software.compatibility_approve", target=row.software,
        after={"major": row.release.major},
    )
    messages.success(
        request,
        f"AlmaLinux {row.release.major} now shows on {row.software.name}.",
    )
    return HttpResponseRedirect(reverse("review:queue"))


@reviewer_required
@require_POST
def software_major_reject(request: HttpRequest, pk: int) -> HttpResponse:
    """Turn down a reported major, deleting the row.

    Deleted rather than parked as rejected so the same major can be reported again
    once the product genuinely does work there.
    """
    row = get_object_or_404(SoftwareCompatibility, pk=pk)
    software, major = row.software, row.release.major
    try:
        software_services.reject_reported_major(
            software=software, release=row.release, by=request.user,
            reason=request.POST.get("reason", ""),
        )
    except ValueError as exc:
        messages.error(request, str(exc))
        return HttpResponseRedirect(reverse("review:queue"))
    messages.info(
        request, f"Dropped the AlmaLinux {major} report on {software.name}."
    )
    return HttpResponseRedirect(reverse("review:queue"))


@reviewer_required
@require_POST
def software_edit_approve(request: HttpRequest, pk: int) -> HttpResponse:
    proposal = get_object_or_404(SoftwareEditProposal, pk=pk)
    before = {field: getattr(proposal.software, field)
              for field in proposal._COPIED_FIELDS}
    try:
        proposal.approve(by=request.user)
    except ValueError as exc:
        messages.error(request, str(exc))
        return HttpResponseRedirect(reverse("review:queue"))
    proposal.software.refresh_from_db()
    log_action(
        "software_edit.approve", target=proposal, before=before,
        after={field: getattr(proposal.software, field)
               for field in proposal._COPIED_FIELDS},
    )
    messages.success(request, f"{proposal.software.name} updated.")
    return HttpResponseRedirect(reverse("review:queue"))


@reviewer_required
@require_POST
def software_edit_reject(request: HttpRequest, pk: int) -> HttpResponse:
    proposal = get_object_or_404(SoftwareEditProposal, pk=pk)
    reason = request.POST.get("reason", "")
    try:
        proposal.reject(by=request.user, reason=reason)
    except ValueError as exc:
        messages.error(request, str(exc))
        return HttpResponseRedirect(reverse("review:queue"))
    log_action("software_edit.reject", target=proposal, notes=reason)
    messages.info(request, "Edit proposal rejected.")
    return HttpResponseRedirect(reverse("review:queue"))


@reviewer_required
def survey_submission_detail(request: HttpRequest, pk: int) -> HttpResponse:
    """One survey submission in full, so a reviewer can judge it rather than guess.

    The queue row carries a machine name and little else, and a moderation decision -
    is this plausible? a duplicate? spam? - needs the payload behind it. Shows the
    extracted facets the statistics will count, the provenance, the access-controlled
    identity with a duplicate signal, and the verbatim inventory.
    """
    import json

    from lumina.survey.models import SurveySubmission

    sub = get_object_or_404(
        SurveySubmission.objects.select_related("submitter", "token", "reviewed_by"),
        pk=pk,
    )
    # How many other submissions share this machine's identity. The one number that
    # says "this box has reported N times" without opening the rollup.
    siblings = (
        SurveySubmission.objects.filter(identity_hash=sub.identity_hash)
        .exclude(pk=sub.pk).count()
        if sub.identity_hash else 0
    )
    from lumina.survey.devices import device_view

    return render(request, "review/survey_detail.html", {
        "sub": sub,
        "siblings": siblings,
        "devices": device_view(sub),
        "inventory_json": json.dumps(sub.inventory or {}, indent=2, sort_keys=True),
    })
