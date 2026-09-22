"""Reviewer dashboard + submission-action views.

The review UI lives here (not in the Django admin) so reviewers don't need
staff status and so the UX can be tuned to the review workflow. Admins can
still reach the admin site for system-configuration tasks (categories,
vendors, users).
"""
from __future__ import annotations

import json
import logging
import uuid

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Count
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from lumina.audit.services import log_action
from lumina.core.certification import ValidationLevel
from lumina.hardware.forms import ReviewerListingEditForm
from lumina.hardware.models import (
    ComponentNamingRule,
    ListingEditProposal,
    Submission,
)
from lumina.hardware.services import annotate_similar_listings, similar_listings
from lumina.notifications.services import emit
from lumina.results.services import stalled_listings
from lumina.review.permissions import reviewer_required
from lumina.software import services as software_services
from lumina.software.models import (
    SoftwareCompatibility,
    SoftwareEditProposal,
    SoftwareSubmission,
)
from lumina.taxonomy.models import CategoryValue
from lumina.vendors.models import VendorClaim, VendorProposal

logger = logging.getLogger(__name__)

# Every reviewable model now inherits OPEN_STATUSES from ReviewWorkflow. Three of
# the four used to lack it, which is why this file re-derived the same tuple by hand
# three times - and why a change to what "open" means would have had to be made here
# as well as in the models.
# The queue's panes, by the slug static/js/tab-url.js uses: a pane id with "tab-" dropped.
# Named here so a redirect cannot invent a tab that does not exist, and so adding a pane
# is one place to change rather than a hunt through the decision views.
QUEUE_TABS = frozenset({
    "submissions", "vendors", "edits", "software",
    "validation-runs", "benchmark-runs", "quarantined-runs",
    "survey-tokens", "survey",
})


def queue_redirect(tab: str = "") -> HttpResponseRedirect:
    """Back to the review queue, on the tab the decision was made from.

    Every decision used to redirect to a bare ``review:queue``, which reopens the first
    tab: a reviewer working through the survey pane was thrown back to hardware
    submissions after each accept, and had to find their place again. The tab is a query
    parameter rather than a fragment because it has to survive a redirect and be read on
    the server side of nothing at all - ``tab-url.js`` opens it on load.
    """
    url = reverse("review:queue")
    if tab and tab in QUEUE_TABS:
        url = f"{url}?tab={tab}"
    return HttpResponseRedirect(url)


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
    # Not a queue of submitted work: the catalog disagreeing with itself. An approved run that
    # published nothing used to be visible only on its submitter's own dashboard, in a status
    # column, so six components of one machine sat as drafts with nobody else able to see it.
    stalled_systems, stalled_components = stalled_listings()
    stalled = [
        *stalled_systems.select_related("vendor"),
        *stalled_components.select_related("vendor"),
    ]
    return render(
        request,
        "review/queue.html",
        {
            "submissions": submissions,
            "stalled_certifications": stalled,
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
    from lumina.audit.models import AuditLogEntry
    from lumina.core.pagination import paginate, paginate_rows
    from lumina.review import archive as archive_service

    rows = archive_service.decisions()
    kind = request.GET.get("kind", "")
    if kind:
        rows = [r for r in rows if r["kind"] == kind]
    # Both panes page and search. The decisions pane did neither: it rendered every row it had,
    # which is fine at ten decisions and a page nobody can read at a thousand.
    decisions = paginate_rows(request, rows, "decisions", text=archive_service.decision_text)

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
    activity = paginate(request, entries, "activity", search=("action", "notes"))

    return render(
        request,
        "review/archive.html",
        {
            "decisions": decisions,
            "activity": activity,
            "kinds": archive_service.decision_kinds(),
            "selected_kind": kind,
            # Whether the per-source bound is actually hiding anything, said out loud rather than
            # left as a list that quietly stops.
            "decisions_capped": len(rows) >= archive_service.DECISION_LIMIT,
            "decision_limit": archive_service.DECISION_LIMIT,
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
    return queue_redirect("submissions")


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
    return queue_redirect("submissions")


@reviewer_required
@require_POST
def request_changes(request: HttpRequest, pk: int) -> HttpResponse:
    submission = get_object_or_404(Submission, pk=pk)
    reason = request.POST.get("reason", "")
    submission.request_changes(by=request.user, reason=reason)
    log_action("submission.request_changes", target=submission, notes=reason)
    emit("submission.needs_changes", target=submission, actor=request.user)
    return queue_redirect("submissions")


@reviewer_required
@require_POST
def promote_value(request: HttpRequest, pk: int) -> HttpResponse:
    value = get_object_or_404(CategoryValue, pk=pk)
    value.approve(by=request.user)
    log_action("taxonomy.value.approve", target=value)
    return queue_redirect("submissions")


@reviewer_required
@require_POST
def reject_value(request: HttpRequest, pk: int) -> HttpResponse:
    value = get_object_or_404(CategoryValue, pk=pk)
    value.reject(by=request.user)
    log_action("taxonomy.value.reject", target=value)
    return queue_redirect("submissions")


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
    return queue_redirect("vendors")


@reviewer_required
@require_POST
def vendor_proposal_reject(request: HttpRequest, pk: int) -> HttpResponse:
    proposal = get_object_or_404(VendorProposal, pk=pk)
    reason = request.POST.get("reason", "")
    proposal.reject(by=request.user, reason=reason)
    log_action("vendor_proposal.reject", target=proposal, notes=reason)
    return queue_redirect("vendors")


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
    emit("proposal.decided", target=proposal, actor=request.user)
    return queue_redirect("edits")


@reviewer_required
@require_POST
def listing_edit_reject(request: HttpRequest, pk: int) -> HttpResponse:
    proposal = get_object_or_404(ListingEditProposal, pk=pk)
    reason = request.POST.get("reason", "")
    proposal.reject(by=request.user, reason=reason)
    log_action("listing_edit.reject", target=proposal, notes=reason)
    emit("proposal.decided", target=proposal, actor=request.user)
    return queue_redirect("edits")


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
        return queue_redirect("vendors")
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
    return queue_redirect("vendors")


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
        return queue_redirect("vendors")
    log_action(
        "vendor_claim.reject", target=claim, before=before,
        after={"status": claim.status}, notes=reason,
    )
    emit("vendor_claim.decided", target=claim, actor=request.user)
    messages.info(request, f"Claim on {claim.vendor.name} rejected.")
    return queue_redirect("vendors")


@reviewer_required
@require_POST
def vendor_claim_request_changes(request: HttpRequest, pk: int) -> HttpResponse:
    claim = get_object_or_404(VendorClaim, pk=pk)
    reason = request.POST.get("reason", "")
    try:
        claim.request_changes(by=request.user, reason=reason)
    except ValueError as exc:
        messages.error(request, str(exc))
        return queue_redirect("vendors")
    log_action("vendor_claim.request_changes", target=claim, notes=reason)
    emit("vendor_claim.decided", target=claim, actor=request.user)
    messages.info(request, "Sent back to the claimant.")
    return queue_redirect("vendors")


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
        return queue_redirect("survey-tokens")
    log_action("survey_token_request.approve", target=req, before=before,
               after={"status": req.status})
    emit("survey_token_request.decided", target=req, actor=request.user)
    messages.success(request, f"{req.requester} can now mint long-lived survey tokens.")
    return queue_redirect("survey-tokens")


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
        return queue_redirect("survey-tokens")
    log_action("survey_token_request.reject", target=req, notes=reason)
    emit("survey_token_request.decided", target=req, actor=request.user)
    messages.info(request, "Survey token request rejected.")
    return queue_redirect("survey-tokens")


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
        return queue_redirect("survey-tokens")
    log_action("survey_token_request.request_changes", target=req, notes=reason)
    emit("survey_token_request.decided", target=req, actor=request.user)
    messages.info(request, "Sent back to the requester.")
    return queue_redirect("survey-tokens")


@reviewer_required
@require_POST
def survey_blacklist_device(request: HttpRequest, pk: int) -> HttpResponse:
    """Blacklist a part from a survey submission, the same rule the run pages create.

    The census counts the devices the catalog does, through the same exclusion rules, and a
    reviewer reading a survey submission is often the first person to see a part nobody wants
    counted. Until now they could see it was counted and had nowhere to say otherwise.
    """
    from lumina.review.run_views import blacklist_device
    from lumina.survey.models import SurveySubmission

    sub = get_object_or_404(SurveySubmission, pk=pk)
    blacklist_device(request)
    return redirect("review:survey_submission_detail", pk=sub.pk)


def _posted_conditions(request) -> list[dict]:
    """The condition rows a form posted, from either shape it can send them in.

    The builder posts parallel ``condition_field``/``condition_op``/``condition_value`` lists, one
    entry per row, because that is what a form of repeatable rows produces. A JSON ``conditions``
    field is accepted too, which is what the preview posted before the rows existed and what a
    script would send.
    """
    fields = request.POST.getlist("condition_field")
    if fields:
        ops = request.POST.getlist("condition_op")
        values = request.POST.getlist("condition_value")
        return [
            {"field": field.strip(), "op": (ops[i] if i < len(ops) else "eq"),
             "value": (values[i] if i < len(values) else "").strip()}
            for i, field in enumerate(fields) if field.strip()
        ]
    try:
        return json.loads(request.POST.get("conditions") or "[]")
    except ValueError:
        return []


@reviewer_required
def naming_rule_form(request: HttpRequest, pk: int | None = None) -> HttpResponse:
    """Write a naming rule, or change one that is already there.

    A new rule is prefilled from the device the reviewer pressed "Name it" on, so the common case
    is typing one template and pressing Save. An existing one is prefilled from itself, which is
    the other half of the same idea: a rule that turns out too broad is a row somebody can go back
    to, and before this the only way to change one was the Django admin, which a reviewer cannot
    reach.

    Saving writes the rule and renames nothing: the plan page is where it takes effect, which
    keeps "I think this is right" and "do it to the catalog" as two decisions.
    """
    from lumina.results import naming

    rule = get_object_or_404(ComponentNamingRule, pk=pk) if pk is not None else None

    if request.method == "POST":
        draft = rule or ComponentNamingRule(created_by=request.user)
        draft.kind = request.POST.get("kind", "")
        draft.match_field = request.POST.get("match_field", "").strip()
        draft.model_pattern = request.POST.get("model_pattern", "").strip()
        draft.conditions = _posted_conditions(request)
        draft.build = request.POST.get("build", "").strip()
        draft.fallback = request.POST.get("fallback", "").strip()
        draft.notes = request.POST.get("notes", "").strip()
        draft.enabled = bool(request.POST.get("enabled"))
        errors: dict[str, list[str]] = {}
        priority = _posted_priority(request)
        if priority is None:
            errors["priority"] = ["Must be a whole number."]
        else:
            draft.priority = priority
        try:
            draft.full_clean(exclude=["created_by"])
        except ValidationError as exc:
            errors = {**errors, **exc.message_dict}
        if errors:
            for field, problems in errors.items():
                messages.error(request, f"{field}: {' '.join(problems)}")
            context, _current = _device_context(request.POST)
            return render(request, "review/naming_rule_new.html", {
                "rule": rule,
                "prefill": request.POST,
                "conditions": draft.conditions,
                "field_groups": _field_groups(context, request.POST.get("kind", "")),
                "operators": sorted(naming.CONDITION_OPS),
                "kinds": _rule_kinds(),
                "source": _source_params(request.POST),
            })
        draft.save()
        log_action(
            f"component_naming_rule.{'update' if rule else 'create'}", target=draft,
            after={"build": draft.build, "priority": draft.priority, "enabled": draft.enabled},
        )
        messages.success(request, (
            "Rule saved. It names parts from now on; press Apply on this page to rename what is "
            "already catalogued."
        ))
        return redirect("review:naming_plan")

    if rule is not None:
        # Editing: the rule is its own prefill, and the field list falls back to the vocabulary
        # since there is no one device an existing rule belongs to.
        return render(request, "review/naming_rule_new.html", {
            "rule": rule,
            "prefill": {
                "kind": rule.kind, "match_field": rule.match_field,
                "model_pattern": rule.model_pattern, "build": rule.build,
                "fallback": rule.fallback, "priority": rule.priority, "notes": rule.notes,
                "enabled": rule.enabled,
            },
            "conditions": rule.conditions or [],
            "field_groups": _field_groups({}, rule.kind),
            "operators": sorted(naming.CONDITION_OPS),
            "kinds": _rule_kinds(),
            "source": {},
        })

    # Opened from a device: start from what was detected, so the reviewer tweaks rather than
    # composes. The conditions are the ids that make this part this part, the template is the name
    # it currently gets, and the field list beside them is every path this machine reports with
    # the value it holds - which is the difference between "what can I match on" and "what does
    # this machine actually say".
    context, current = _device_context(request.GET)
    suggested = _suggested(request.GET)
    return render(request, "review/naming_rule_new.html", {
        "rule": None,
        "prefill": {
            "kind": request.GET.get("kind", ""),
            # Opened from a submitter's answer: match the field their value came from, and build
            # the name out of what it captured. The whole point of recording the path rather than
            # the string alone - a rule from it fixes every machine of that shape.
            "match_field": suggested["field"],
            "model_pattern": suggested["pattern"],
            "build": suggested["build"] or current,
            "fallback": "",
            "priority": 50,
            "notes": "",
            "enabled": True,
        },
        "conditions": naming.suggested_conditions(context) if context else [],
        "field_groups": _field_groups(context, request.GET.get("kind", "")),
        "operators": sorted(naming.CONDITION_OPS),
        "kinds": _rule_kinds(),
        "source": _source_params(request.GET),
    })


@reviewer_required
@require_POST
def naming_rule_delete(request: HttpRequest, pk: int) -> HttpResponse:
    """Remove a rule for good.

    Disabling is the reversible way to stop a rule, and is what the form offers. This is for one
    that should not have been written at all. Parts it already named keep their names - a rename
    was applied to the catalog and undoing it is a separate decision - and only the record of
    which rule chose them goes.
    """
    rule = get_object_or_404(ComponentNamingRule, pk=pk)
    log_action("component_naming_rule.delete", target=rule,
               before={"build": rule.build, "priority": rule.priority})
    rule.delete()
    messages.success(request, "Rule deleted. Parts it already named keep their names.")
    return redirect("review:naming_plan")


def _posted_priority(request) -> int | None:
    """The priority as posted, or None for something that is not a whole number.

    ``int()`` on a reviewer's typo was a 500 that lost everything else they had typed, on a form
    where the rest of the work is the part worth keeping.
    """
    raw = (request.POST.get("priority") or "").strip()
    if not raw:
        return 100
    try:
        return int(raw)
    except ValueError:
        return None


def _suggested(params) -> dict:
    """A submitter's answer, as the start of a rule.

    The pattern is the whole value anchored, with its one capture, so the rule says "when this
    field reads exactly what they told us, that is the name". Widening it to a family of machines
    is the reviewer's edit to make, and a pattern that matches one machine is the safe place to
    start from.
    """
    import re as _re

    from lumina.results.models import TestRun

    blank = {"field": "", "pattern": "", "build": ""}
    key = params.get("suggested")
    if not key:
        return blank
    try:
        run = TestRun.objects.filter(uuid=uuid.UUID(params.get("run") or "")).first()
    except ValueError:
        return blank
    answer = ((run.component_suggestions if run else None) or {}).get(key)
    if not answer:
        return blank
    # The stored path names one structure ("dmi.processor.0.Part Number"); the rule should not
    # care which one, so the index becomes a wildcard.
    field = _re.sub(r"\.\d+(?=\.|$)", ".*", answer.get("path") or "")
    return {
        "field": field,
        "pattern": "^(" + _re.escape(answer.get("value") or "") + ")$",
        "build": "{{ match.1 }}",
    }


def _rule_kinds() -> list[tuple[str, str]]:
    """The kinds a rule may be written for, marking the ones nothing consults yet.

    Only GPU naming goes through the engine today. A rule for another kind saves, validates, and
    then does nothing at all, which is worse than being told: the reviewer has no way to tell a
    rule that does not apply from one whose match is wrong.
    """
    from lumina.hardware.models import ComponentKind
    from lumina.results import naming

    return [
        (value, label if value in naming.RULED_KINDS else f"{label} (not applied yet)")
        for value, label in ComponentKind.choices()
    ]


def _source_params(params) -> dict[str, str]:
    """The run or submission the builder was opened from, to carry forward as hidden fields.

    A mapping rather than two named fields, so a page that gains a third kind of source does not
    have to be threaded through the form again.
    """
    return {key: params[key] for key in ("run", "submission") if params.get(key)}


def _naming_source(params):
    """The run or submission the reviewer pressed "Name it" on, or None.

    Either kind of page, because the collected payload underneath a validate run, a benchmark run,
    and a survey submission is the same and a reviewer who can write a rule from one should be able
    to write it from the others. Both carry ``inventory`` and ``cpu_model``, which is all the
    device helpers read.
    """
    from lumina.results.models import TestRun
    from lumina.survey.models import SurveySubmission

    try:
        if params.get("run"):
            return TestRun.objects.filter(uuid=uuid.UUID(params["run"])).first()
        if params.get("submission"):
            return SurveySubmission.objects.filter(pk=int(params["submission"])).first()
    except (ValueError, TypeError):
        # A hand-edited query string, and a 500 here loses whatever the reviewer had typed.
        return None
    return None


def _named_devices(source, kind: str) -> list[tuple[dict, str]]:
    """``(device, what it is called now)`` for the parts of one kind, as the page showed them.

    The name is the page's own, not a second derivation of it: the point of prefilling is that the
    reviewer edits the string they are looking at.
    """
    from lumina.results.component_match import gpu_display_name, name_status
    from lumina.results.device_inventory import categorized_devices
    from lumina.results.pci_names import nic_identity, pci_name

    # The kinds the firmware reports as two strings rather than as a device on a bus. They have no
    # PCI identity, so a rule for one matches on the strings themselves and the builder has to be
    # reachable without any ids in the link.
    if kind in ("cpu", "motherboard"):
        vendor, model = _reported_part(source, kind)
        if not model:
            return []
        device = {"vendor": vendor, "model": model}
        payload = getattr(source, "inventory", None) or {}
        return [(device, name_status(kind, vendor, model, payload)["name"])]
    if kind == "gpu":
        gpus = [d for d in categorized_devices(source)["gpus"] if isinstance(d, dict)]
        cpu_model = getattr(source, "cpu_model", "") or ""
        return [(gpu, gpu_display_name(gpu, cpu_model, gpus)) for gpu in gpus]
    if kind == "nic":
        nics = [d for d in categorized_devices(source)["nics"] if isinstance(d, dict)]
        return [(nic, nic_identity(nic)[1]) for nic in nics]
    # Every other kind: the raw enumeration, named by pci.ids, which is what those rows show.
    summary = (getattr(source, "inventory", None) or {}).get("summary") or {}
    others = [d for d in (summary.get("pci_devices") or []) if isinstance(d, dict)]
    return [(d, pci_name((d.get("pci_ids") or {}).get("device"))) for d in others]


def _reported_part(source, kind: str) -> tuple[str, str]:
    """The vendor and model a run reported for a CPU or a motherboard."""
    from lumina.hardware.models import ComponentKind
    from lumina.results.services import cpu_brand

    if kind == ComponentKind.cpu.value:
        # ``cpu_brand`` maps the CPUID string to a real manufacturer ("GenuineIntel" to "Intel"),
        # which is what the catalog files the part under and so what a rule should see.
        return cpu_brand(source), source.cpu_model or ""
    return (source.board_vendor or "", source.board_model or "")


def _device_context(params) -> tuple[dict, str]:
    """The context and current name for the device a reviewer pressed "Name it" on.

    Located by its PCI ids on the source's own devices rather than passed as a blob, so the page
    cannot be handed a device that machine never reported.
    """
    from lumina.results import naming

    source = _naming_source(params)
    if source is None:
        return {}, ""
    kind = params.get("kind", "")
    named = _named_devices(source, kind)
    devices = [device for device, _name in named]
    if kind in ("cpu", "motherboard"):
        # One per machine and no ids to match on, so the kind is the whole address.
        for device, name in named:
            return naming.build_context(kind, device, source.inventory or {}, devices), name
        return {}, ""
    wanted = (params.get("vendor_id", ""), params.get("device_id", ""))
    for device, name in named:
        context = naming.build_context(kind, device, source.inventory or {}, devices)
        if (context["pci"]["vendor_id"], context["pci"]["device_id"]) == wanted:
            return context, name
    return {}, ""


def _field_groups(context: dict, kind: str) -> list[tuple[str, list[dict]]]:
    """What a rule may read, grouped, whether or not there is a machine to read it from.

    With a device, the values it reported. Without one - the builder opened from the naming page,
    or on a part the source never reported - the vocabulary alone, because "what can I match on"
    is a question the form has to answer either way. A reviewer who cannot see what is under
    ``pci`` is back to guessing at path names.
    """
    from lumina.results import naming

    return naming.field_groups(naming.available_fields(
        context or naming.build_context(kind, {}, {})))


@reviewer_required
def naming_plan(request: HttpRequest) -> HttpResponse:
    """What applying the current naming rules would do to the catalog, before it does it.

    Saving a rule changes nothing. This is where a reviewer sees the consequence: which components
    would be renamed, which would collide with a name already taken, and which have runs that
    disagree with each other. A rule that matches more broadly than its author meant shows up here
    as a list of parts they did not intend to touch, which is the point of looking first.
    """
    from lumina.results import naming

    plan = naming.rename_plan()
    return render(request, "review/naming_plan.html", {
        "plan": plan,
        # Disabled rules included: they are not applied, but this is the page a reviewer would
        # turn one back on from, and one that hides them has no way back.
        "rules": ComponentNamingRule.objects.order_by("priority", "id"),
    })


@reviewer_required
@require_POST
def naming_apply(request: HttpRequest) -> HttpResponse:
    """Perform the unambiguous renames. Collisions and conflicts are reported, not guessed at."""
    from lumina.results import naming

    plan = naming.rename_plan()
    applied = naming.apply_plan(plan)
    left = len(plan["collisions"]) + len(plan["conflicts"])
    messages.success(request, (
        f"Renamed {applied} component(s)."
        + (f" {left} left for a person: a name already taken, or runs that disagree." if left
           else "")
    ))
    return redirect("review:naming_plan")


@reviewer_required
@require_POST
def naming_preview(request: HttpRequest) -> JsonResponse:
    """A draft rule against one run's devices, as the reviewer types it.

    Answers the two questions somebody writing a rule has: what would this call the part in front
    of me, and how much else would it touch. The second is the one that matters - "this looks
    right" and "this renames 412 machines" are different decisions, and only one of them is
    visible without asking.
    """
    from lumina.hardware.models import ComponentNamingRule
    from lumina.results import naming
    from lumina.results.models import TestRun

    # Carrying the pk of the rule being edited, so the ambiguity check excludes it the way it
    # does on save. Without it every preview from an edit page found that rule and reported it as
    # its own duplicate, which made Preview useless for exactly the rules worth previewing.
    editing = (request.POST.get("rule") or "").strip()
    draft = ComponentNamingRule(
        pk=int(editing) if editing.isdigit() else None,
        kind=request.POST.get("kind", ""),
        match_field=request.POST.get("match_field", ""),
        model_pattern=request.POST.get("model_pattern", ""),
        conditions=_posted_conditions(request),
        build=request.POST.get("build", ""),
        fallback=request.POST.get("fallback", ""),
        priority=_posted_priority(request) or 100,
    )
    try:
        draft.clean()
    except ValidationError as exc:
        return JsonResponse({"ok": False, "errors": exc.message_dict}, status=200)

    # Parsed before it reaches the query: a UUIDField raises on anything that is not one, and the
    # field is empty whenever the builder is opened from the naming page rather than from a device.
    # Previewing with nothing to preview against is an ordinary thing to do, not a server error.
    try:
        run = TestRun.objects.filter(uuid=uuid.UUID(request.POST.get("run") or "")).first()
    except ValueError:
        run = None
    names = []
    if run is not None:
        payload = run.inventory or {}
        # Against the parts of the kind the rule is for. This previewed a run's GPUs whatever the
        # rule was about, so a CPU rule was tried on graphics cards and reported "matches nothing"
        # however right it was - which is what a preview is for and exactly when it must not lie.
        for kind in ([draft.kind] if draft.kind else naming.RULED_KINDS):
            named = _named_devices(run, kind)
            devices = [device for device, _name in named]
            for device, current in named:
                context = naming.build_context(kind, device, payload, devices)
                name, _rule = naming.apply_rules(context, [draft])
                names.append({
                    "reported": device.get("reported_model") or current,
                    "named": name or current, "matched": bool(name),
                })
    # How many components the rule would rename if it were saved and applied. Counted against the
    # draft alone, so it answers "what does *this* rule do" rather than "what does the table do".
    affected = _naming_reach(draft)
    return JsonResponse({"ok": True, "devices": names, "affected": affected})


def _naming_reach(draft) -> int:
    """How many catalogued components this draft rule would rename.

    Over the runs that produced them, because a component does not keep the device that made it.
    Capped: this runs on every keystroke and a census-sized catalog is not worth re-resolving to
    answer a question whose useful form is "a few" or "hundreds".
    """
    from lumina.results import naming
    from lumina.results.models import TestRun

    seen: set[int] = set()
    for run in TestRun.objects.order_by("-id")[:_NAMING_REACH_RUNS].iterator():
        # The plan's own machinery, asked about this draft alone. It counted GPUs only, and it
        # counted them by trying every device against every component, so a CPU rule reached
        # nothing and a GPU rule could count a component twice over.
        for component, name, _rule in naming._component_names(run, rules=[draft]):
            if name != component.name:
                seen.add(component.pk)
    return len(seen)


# A sample rather than the whole catalog: this answers a live preview, and the number a reviewer
# needs is an order of magnitude, not an audit.
_NAMING_REACH_RUNS = 200


@reviewer_required
@require_POST
def survey_facets_rebuild(request: HttpRequest) -> HttpResponse:
    """Rewrite the derived facet columns from each submission's stored payload.

    The columns are an index - segments filter on them, the admin searches them - and they hold
    what a naming rule said on the day each machine reported. A rule corrected today fixes the
    published statistics on the next rollup, because the rollup derives from the payload, and
    leaves those columns saying the old thing until this runs. A segment and a page would then
    disagree about the same machine, which is the failure this exists to prevent.

    Beside the statistics rebuild rather than in a shell, because the person who needs it is the
    reviewer who just noticed the two disagreeing. Synchronous and guarded the same way, so a
    double-click cannot start two.
    """
    from django.core.cache import cache

    from lumina.survey import facets

    if not cache.add("survey:facets-running", True, 600):
        messages.info(request, "A facet rebuild is already running. Give it a moment.")
        return queue_redirect("survey")
    try:
        changed = facets.rebuild()
    except Exception:
        logger.exception("manual survey facet rebuild failed")
        messages.error(
            request,
            "The facet rebuild failed. Nothing was lost: the submitted payloads are untouched "
            "and the published statistics derive from them either way.",
        )
        return queue_redirect("survey")
    finally:
        cache.delete("survey:facets-running")

    messages.success(request, (
        f"Facet columns rewritten for {changed} submission(s). "
        "Rebuild the statistics as well if a naming rule changed."
    ) if changed else "Facet columns were already in step with the payloads.")
    return queue_redirect("survey")


@reviewer_required
@require_POST
def survey_rollup_now(request: HttpRequest) -> HttpResponse:
    """Rebuild the published survey aggregates on demand.

    The statistics page reads the rollup, never the submissions, so nothing a reviewer
    does shows up there until a rebuild runs. Two timers handle that (the current month
    hourly, everything nightly), which is fine in steady state and no help at all in the
    two situations where somebody is actually watching: just after a deploy that changes
    how a facet is derived, when every historical row is stale, and while checking whether
    a machine that was just submitted came through.

    Synchronous on purpose, so the reviewer sees the result rather than being told it will
    happen. That is also why "everything" is a separate button from "this month": one is a
    couple of periods and the other is every period the census has, and the second is the
    one that can take a while.
    """
    from django.core.cache import cache

    from lumina.survey.management.commands.survey_rollup import current_month
    from lumina.survey.services import rebuild_survey_stats

    everything = request.POST.get("scope") == "all"
    # A rebuild deletes and recreates a period's rows, so two at once contend for the same
    # rows. The timers take a flock; a double-clicked button needs its own guard, and
    # ``cache.add`` is atomic on the shared cache. Refused rather than queued: the answer
    # to "did that work" is the same either way, and a queue of rebuilds is pure waste.
    if not cache.add("survey:rollup-running", True, 600):
        messages.info(
            request, "A statistics rebuild is already running. Give it a moment."
        )
        return queue_redirect("survey")
    try:
        periods = rebuild_survey_stats(period=None if everything else current_month())
    except Exception:
        logger.exception("manual survey rollup failed")
        messages.error(
            request,
            "The statistics rebuild failed. Nothing was lost: the submissions are "
            "untouched and the scheduled rebuild will try again.",
        )
        return queue_redirect("survey")
    finally:
        cache.delete("survey:rollup-running")

    # No audit entry: ``log_action`` records a decision about one object and a rollup is
    # about none, so there is nothing to hang it on. It also changes no submission - the
    # raw layer is untouched and the aggregate is derived - so there is nothing here that
    # a reviewer could be asked to account for later. The scheduled rebuilds log nothing
    # either, for the same reason.
    scope = "every period" if everything else "the current month"
    rebuilt = ", ".join(periods) or "no periods, because nothing has been submitted yet"
    messages.success(request, f"Statistics rebuilt for {scope}: {rebuilt}.")
    return queue_redirect("survey")


@reviewer_required
@require_POST
def survey_submission_accept(request: HttpRequest, pk: int) -> HttpResponse:
    """Acknowledge a survey submission - reviewed, kept in stats."""
    from lumina.survey.models import SurveySubmission
    from lumina.survey.services import moderate_submission

    sub = get_object_or_404(SurveySubmission, pk=pk)
    if sub.follows_run:
        # No button offers this. A stale page or a hand-made request can still arrive, and
        # the honest answer names where the decision does belong.
        messages.info(
            request,
            "That submission came from a certification run and is decided with that run, "
            "not on its own.",
        )
        return queue_redirect("survey")
    moderate_submission(sub, by=request.user, dismiss=False)
    log_action("survey_submission.accept", target=sub, actor=request.user)
    emit("survey_submission.decided", target=sub, actor=request.user)
    messages.success(request, "Survey submission accepted.")
    return queue_redirect("survey")


@reviewer_required
@require_POST
def survey_submission_dismiss(request: HttpRequest, pk: int) -> HttpResponse:
    """Exclude a survey submission from published stats (spam or anomaly)."""
    from lumina.survey.models import SurveySubmission
    from lumina.survey.services import moderate_submission

    sub = get_object_or_404(SurveySubmission, pk=pk)
    if sub.follows_run:
        # No button offers this. A stale page or a hand-made request can still arrive, and
        # the honest answer names where the decision does belong.
        messages.info(
            request,
            "That submission came from a certification run and is decided with that run, "
            "not on its own.",
        )
        return queue_redirect("survey")
    moderate_submission(sub, by=request.user, dismiss=True)
    log_action("survey_submission.dismiss", target=sub, actor=request.user)
    emit("survey_submission.decided", target=sub, actor=request.user)
    messages.info(request, "Survey submission dismissed (excluded from stats).")
    return queue_redirect("survey")


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
        return queue_redirect("software")
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
    return queue_redirect("software")


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
        return queue_redirect("software")
    log_action("software.reject", target=submission, before=before,
               after={"status": submission.status}, notes=reason)
    emit("submission.rejected", target=submission, actor=request.user)
    messages.info(request, "Software submission rejected.")
    return queue_redirect("software")


@reviewer_required
@require_POST
def software_request_changes(request: HttpRequest, pk: int) -> HttpResponse:
    submission = get_object_or_404(SoftwareSubmission, pk=pk)
    reason = request.POST.get("reason", "")
    try:
        submission.request_changes(by=request.user, reason=reason)
    except ValueError as exc:
        messages.error(request, str(exc))
        return queue_redirect("software")
    log_action("software.request_changes", target=submission, notes=reason)
    emit("submission.needs_changes", target=submission, actor=request.user)
    messages.info(request, "Sent back to the submitter.")
    return queue_redirect("software")


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
        return queue_redirect("software")
    log_action(
        "software.compatibility_approve", target=row.software,
        after={"major": row.release.major},
    )
    # The row rather than the software, because the row carries who proposed it and the software
    # does not. The audit entry stays on the software, which is the thing that changed.
    emit("proposal.decided", target=row, actor=request.user)
    messages.success(
        request,
        f"AlmaLinux {row.release.major} now shows on {row.software.name}.",
    )
    return queue_redirect("software")


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
        return queue_redirect("software")
    messages.info(
        request, f"Dropped the AlmaLinux {major} report on {software.name}."
    )
    return queue_redirect("software")


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
        return queue_redirect("software")
    proposal.software.refresh_from_db()
    log_action(
        "software_edit.approve", target=proposal, before=before,
        after={field: getattr(proposal.software, field)
               for field in proposal._COPIED_FIELDS},
    )
    emit("proposal.decided", target=proposal, actor=request.user)
    messages.success(request, f"{proposal.software.name} updated.")
    return queue_redirect("software")


@reviewer_required
@require_POST
def software_edit_reject(request: HttpRequest, pk: int) -> HttpResponse:
    proposal = get_object_or_404(SoftwareEditProposal, pk=pk)
    reason = request.POST.get("reason", "")
    try:
        proposal.reject(by=request.user, reason=reason)
    except ValueError as exc:
        messages.error(request, str(exc))
        return queue_redirect("software")
    log_action("software_edit.reject", target=proposal, notes=reason)
    emit("proposal.decided", target=proposal, actor=request.user)
    messages.info(request, "Edit proposal rejected.")
    return queue_redirect("software")


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
    # Where the per-device actions post. Passed in rather than built in the template, so the
    # include is identical here and on the run pages.
    blacklist_url = reverse("review:survey_blacklist_device", args=[pk])
    # How many other submissions share this machine's identity. The one number that
    # says "this box has reported N times" without opening the rollup.
    siblings = (
        SurveySubmission.objects.filter(identity_hash=sub.identity_hash)
        .exclude(pk=sub.pk).count()
        if sub.identity_hash else 0
    )
    from lumina.survey.devices import device_view
    from lumina.survey.explain import explain

    return render(request, "review/survey_detail.html", {
        "sub": sub,
        "siblings": siblings,
        "devices": device_view(sub),
        # The gate-by-gate account of this machine's path into the published figures.
        # Asked here because this is the page somebody is on when they wonder why a card
        # they can see is missing from the statistics; the same reasoning is available as
        # ``manage.py survey_explain`` for a shell.
        "explanation": explain(sub),
        "inventory_json": json.dumps(sub.inventory or {}, indent=2, sort_keys=True),
        "blacklist_url": blacklist_url,
        # So "Name it" on this page prefills from this submission, the way it does from a run.
        # A submission carries the same collected payload; only the link differed.
        "naming_source": f"submission={sub.pk}",
    })
