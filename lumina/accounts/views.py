"""Accounts views: dashboard, device activation, API token management."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db import models
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from lumina.accounts.forms import ActivateForm, ApiTokenCreateForm
from lumina.accounts.models import ApiToken, DeviceAuthRequest
from lumina.audit.services import log_action

_ACTIVATE_ATTEMPT_LIMIT = 5
_ACTIVATE_LOCKOUT_SECONDS = 15 * 60


@login_required
def dashboard(request: HttpRequest) -> HttpResponse:
    """Personal workspace: quick actions plus everything the user has in the
    system - their listings, validation runs, and benchmark runs - each with
    client-side filtering so old work is findable and re-submittable."""
    from lumina.hardware.models import Component, System
    from lumina.hardware.models import Submission as HardwareSubmission
    from lumina.hardware.services import publication_state
    from lumina.results.models import RunType, TestRun
    from lumina.software.models import Software, SoftwareSubmission
    from lumina.vendors.models import VendorClaim
    from lumina.vendors.services import can_edit_listing

    user = request.user
    my_systems = list(
        System.objects.filter(
            models.Q(created_by=user) | models.Q(test_runs__submitter=user)
        )
        .select_related("vendor")
        # publication_state walks both, so fetch them once for the page.
        .prefetch_related("submissions", "test_runs")
        .distinct()
        .order_by("vendor__name", "name")[:200]
    )
    my_components = list(
        Component.objects.filter(
            models.Q(created_by=user) | models.Q(test_runs__submitter=user)
        )
        .select_related("vendor")
        .prefetch_related("submissions", "test_runs")
        .distinct()
        .order_by("kind", "vendor__name", "name")[:200]
    )
    # A hardware submission's own state, hung on the listing it is about, exactly as
    # ``latest_submission`` is for software below.
    #
    # This page queried listings and never ``hardware.Submission`` at all, so a
    # submitter saw their listing and nothing about what had happened to it. A reviewer
    # who sent one back with notes reached nobody: no email, no status, and
    # ``reviewer_notes`` appeared in no submitter-facing template. "Needs changes" was a
    # request addressed to a person who could not read it, and the only way to act on it
    # was to submit again from scratch, which opened a *second* pending row beside the
    # first for a reviewer to clean up by hand.
    #
    # Ascending, so for a listing submitted more than once the newest row survives in
    # the dict - the same ordering trick, and the same reason, as software's.
    hardware_submissions = {}
    for submission in (
        HardwareSubmission.objects.filter(submitter=user)
        .prefetch_related("cited_releases")
        .order_by("submitted_at")
    ):
        listing = submission.listing
        if listing is not None:
            hardware_submissions[(type(listing).__name__, listing.pk)] = submission
    for listing in (*my_systems, *my_components):
        listing.user_can_edit = can_edit_listing(user, listing)
        listing.latest_submission = hardware_submissions.get(
            (type(listing).__name__, listing.pk)
        )
        # Why an unpublished row is unpublished, and what would change it. "Unpublished"
        # on its own is a fact about the column rather than an answer, and for a seeded
        # CPU or GPU family that a run was merely classified against the answer is that
        # it is not the submitter's listing at all.
        listing.publication = publication_state(listing, user)
    # Software the user created or submitted for. Without this a software
    # publisher's own workspace denies their listings exist.
    my_software = list(
        Software.objects.filter(
            models.Q(created_by=user) | models.Q(submissions__submitter=user)
        )
        .select_related("vendor")
        .prefetch_related("compatibility__release")
        .distinct()
        .order_by("vendor__name", "name")[:200]
    )
    # One row per product carrying its own submission state, rather than a second
    # table of submissions beside it. A submission is only ever *about* a product,
    # so two tables asked the reader to join them by name to answer "what is
    # happening with this listing".
    # Ascending, so for a product submitted more than once the newest row is the
    # one that overwrites and survives in the dict.
    latest_submissions = {
        s.software_id: s
        for s in SoftwareSubmission.objects.filter(submitter=user)
        .order_by("submitted_at")
    }
    for product in my_software:
        product.user_can_edit = can_edit_listing(user, product)
        product.latest_submission = latest_submissions.get(product.pk)
    # Open claims, so a claimant can see theirs is still waiting rather than
    # wondering whether it was submitted at all.
    my_vendor_claims = list(
        VendorClaim.objects.filter(
            requester=user, status__in=VendorClaim.OPEN_STATUSES
        )
        .select_related("vendor")
        .order_by("-submitted_at")
    )
    runs = (
        TestRun.objects.filter(submitter=user)
        .select_related("alma_release", "listing_system", "listing_system__vendor")
        .order_by("-received_at")
    )
    # Split four ways: validation and benchmark, each into what the submitter is still working on
    # and what they have put away. Archived work stays reachable and stays theirs; it just stops
    # being the first thing they see every time they open the page.
    def _split(predicate):
        matching = [r for r in runs if predicate(r)]
        return (
            [r for r in matching if r.archived_at is None][:200],
            [r for r in matching if r.archived_at is not None][:200],
        )

    my_validation_runs, my_archived_validation_runs = _split(
        lambda r: r.run_type != RunType.benchmark.value
    )
    my_benchmark_runs, my_archived_benchmark_runs = _split(
        lambda r: r.run_type == RunType.benchmark.value
    )

    return render(
        request,
        "accounts/dashboard.html",
        {
            "my_systems": my_systems,
            "my_components": my_components,
            "my_software": my_software,
            "my_vendor_claims": my_vendor_claims,
            "my_validation_runs": my_validation_runs,
            "my_benchmark_runs": my_benchmark_runs,
            "my_archived_validation_runs": my_archived_validation_runs,
            "my_archived_benchmark_runs": my_archived_benchmark_runs,
            "pending_actions": _pending_actions(user),
        },
    )


def _pending_actions(user) -> list[dict]:
    """Everything waiting on this person, across every kind of thing they can own.

    The dashboard already says what state each listing, run, and claim is in, per section. What it
    could not answer was the question somebody actually opens it with: is there anything for me to
    do. That answer was spread over five tables and three status vocabularies.

    **Only things waiting on the user.** A pending review is information, not a task, and putting
    it here would make the block something to scroll past. So this excludes anything whose next
    move belongs to a reviewer: pending submissions, pending claims, and quarantined runs, which a
    reviewer releases. It excludes terminal states, rejected and approved, for the same reason in
    reverse. And it excludes archived runs, because archiving is precisely the statement that the
    submitter does not intend to act.

    **Queried directly, not read off the display lists.** Those are capped at 200 rows and reduced
    to one submission per listing, either of which can hide an item that needs answering.

    Sorted oldest first: the thing that has waited longest is the thing most likely forgotten.
    """
    from lumina.hardware.models import Submission as HardwareSubmission
    from lumina.results import services
    from lumina.results.models import TestRun
    from lumina.software.models import SoftwareSubmission

    actions: list[dict] = []

    # Runs. Several drafts of one machine are one answer, not nine, so they collapse to the newest
    # of the group, which is the page that offers to submit the whole batch.
    runs = list(
        TestRun.objects.awaiting_submitter(user)
        .select_related("alma_release", "listing_system", "listing_system__vendor")
        .order_by("-received_at")
    )
    grouped: set[int] = set()
    for run in runs:
        if run.pk in grouped:
            continue
        siblings = []
        if run.status == TestRun.STATUS_DRAFT:
            siblings = [
                s for s in services.sibling_draft_runs(run) if s.pk != run.pk
            ]
            grouped.update(s.pk for s in siblings)
        if run.status == TestRun.STATUS_DRAFT:
            # What the run is actually missing, rather than the status label. A draft can have
            # nothing outstanding and still need releasing, which is common enough that saying
            # "listing details are missing" would often be wrong.
            outstanding = services.missing_submission_details(run)
            ask = (
                "Still needed: " + ", ".join(outstanding)
                if outstanding
                else "Complete. It needs releasing for review."
            )
            cta = "Finish submission"
        else:
            ask = "A reviewer asked for changes."
            cta = "Review and resubmit"
        title = run.display_name
        if siblings:
            title = f"{title} ({len(siblings) + 1} runs)"
        actions.append({
            # The kind label is the one line on the card whose job is to say what sort of thing this
            # is, so it carries the scope. A GPU-scoped run filed under a bare "Validation run",
            # titled with the host machine's name, is how the submitter came to be "prompted in the
            # GUI in several different ways as if it is a whole system run": ``display_name`` now
            # names the card, and this says what the card is being claimed for.
            "category": (
                f"{', '.join(run.scope_labels)} validation run"
                if run.is_scoped else "Validation run"
            ),
            "title": title,
            "ask": ask,
            "note": run.reviewer_notes or "",
            "url": run.get_absolute_url(),
            "cta": cta,
            "since": run.received_at,
        })

    for submission in (
        HardwareSubmission.objects.filter(
            submitter=user, status=HardwareSubmission.STATUS_NEEDS_CHANGES,
        )
        .select_related("listing_system", "listing_component")
        .order_by("submitted_at")
    ):
        listing = submission.listing
        actions.append({
            "category": "Hardware listing",
            "title": str(listing) if listing else "Hardware submission",
            "ask": "A reviewer asked for changes.",
            "note": submission.reviewer_notes or "",
            "url": reverse("submit:revise", args=[submission.uuid]),
            "cta": "Revise and resubmit",
            "since": submission.submitted_at,
        })

    for submission in (
        SoftwareSubmission.objects.filter(
            submitter=user, status=SoftwareSubmission.STATUS_NEEDS_CHANGES,
        )
        .select_related("software")
        .order_by("submitted_at")
    ):
        actions.append({
            "category": "Software listing",
            "title": str(submission.software),
            "ask": "A reviewer asked for changes.",
            "note": submission.reviewer_notes or "",
            "url": reverse("software:revise", args=[submission.uuid]),
            "cta": "Revise and resubmit",
            "since": submission.submitted_at,
        })

    # Deliberately not vendor claims. A claim sent back for more evidence *is* waiting on the
    # claimant, but there is nowhere for them to go: ``claim_vendor`` refuses a second claim while
    # one is open, and nothing calls ``VendorClaim.resubmit``. A row in this block with no button
    # is worse than no row, so the existing claims card carries it until that route exists.
    actions.sort(key=lambda item: item["since"])
    return actions


def _attempt_key(request: HttpRequest) -> str:
    return f"activate-attempts:{request.session.session_key or 'anon'}"


@login_required
def activate(request: HttpRequest) -> HttpResponse:
    """Enter the code shown by ``alma-cert register`` and approve the machine.

    Two steps on purpose: after the code matches, the operator sees what they
    are authorizing (client name, requesting IP) before confirming - codes
    are short, so blind approval would let a guessed code steal a session.
    """
    attempts = cache.get(_attempt_key(request), 0)
    if attempts >= _ACTIVATE_ATTEMPT_LIMIT:
        return render(request, "accounts/activate.html", {"locked_out": True})

    form = ActivateForm(request.POST or None, initial={"user_code": request.GET.get("code", "")})
    device_request = None
    if request.method == "POST" and form.is_valid():
        device_request = form.find_request()
        if device_request is None:
            cache.set(_attempt_key(request), attempts + 1, _ACTIVATE_LOCKOUT_SECONDS)
            form.add_error("user_code", "Unknown or expired code.")
    return render(
        request,
        "accounts/activate.html",
        {"form": form, "device_request": device_request},
    )


@login_required
@require_POST
def activate_confirm(request: HttpRequest, pk: int) -> HttpResponse:
    device_request = get_object_or_404(DeviceAuthRequest.objects.pending(), pk=pk)
    decision = request.POST.get("decision")
    if decision == "approve":
        device_request.approve(by=request.user)
        log_action("device_auth.approve", target=device_request)
        messages.success(
            request,
            f"Authorized {device_request.client_name}. "
            "The machine will pick up its token within a few seconds.",
        )
    else:
        device_request.deny(by=request.user)
        log_action("device_auth.deny", target=device_request)
        messages.info(request, "Request denied.")
    return redirect("accounts:dashboard")


@login_required
def tokens(request: HttpRequest) -> HttpResponse:
    form = ApiTokenCreateForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        token, raw = ApiToken.issue(
            user=request.user,
            name=form.cleaned_data["name"],
            scopes=form.cleaned_data["scopes"],
            ttl_seconds=form.cleaned_data["ttl_seconds"],
        )
        log_action("api_token.create", target=token)
        # One-shot flash of the raw value; it is never retrievable again.
        request.session["new_token_raw"] = raw
        request.session["new_token_id"] = token.pk
        return redirect("accounts:tokens")

    new_token_raw = request.session.pop("new_token_raw", None)
    new_token_id = request.session.pop("new_token_id", None)
    return render(
        request,
        "accounts/tokens.html",
        {
            "form": form,
            "tokens": request.user.api_tokens.all(),
            "new_token_raw": new_token_raw,
            "new_token_id": new_token_id,
        },
    )


@login_required
@require_POST
def token_revoke(request: HttpRequest, pk: int) -> HttpResponse:
    token = get_object_or_404(ApiToken, pk=pk, user=request.user)
    token.revoke()
    log_action("api_token.revoke", target=token)
    messages.info(request, f"Token “{token.name}” revoked.")
    return redirect("accounts:tokens")
