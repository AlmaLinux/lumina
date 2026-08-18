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

from lumina.accounts.forms import (
    AccountSettingsForm,
    ActivateForm,
    ApiTokenCreateForm,
)
from lumina.accounts.models import AccountSettings, ApiToken, DeviceAuthRequest
from lumina.audit.services import log_action

_ACTIVATE_ATTEMPT_LIMIT = 5
_ACTIVATE_LOCKOUT_SECONDS = 15 * 60


@login_required
def dashboard(request: HttpRequest) -> HttpResponse:
    """Personal workspace: quick actions plus everything the user has in the system.

    Every list here pages and searches on the server. They used to be cut off at two hundred rows
    with nothing saying so, and the filter boxes matched only against the rows that had survived
    the cut - so a submitter looking for their own run from last year was not told "not on this
    page", they were told it did not exist.
    """
    from lumina.core.models import DisplayDefaults
    from lumina.core.pagination import paginate
    from lumina.hardware.models import Component, System
    from lumina.hardware.models import Submission as HardwareSubmission
    from lumina.hardware.services import publication_state
    from lumina.results.models import RunType, TestRun
    from lumina.software.models import Software, SoftwareSubmission
    from lumina.vendors.models import VendorClaim, VendorMembership
    from lumina.vendors.services import can_edit_listing

    user = request.user
    per_page = DisplayDefaults.per_page()

    def section(queryset, key, *, search=(), per=None):
        return paginate(request, queryset, key, search=search, per_page=per or per_page)

    mine = models.Q(created_by=user) | models.Q(test_runs__submitter=user)
    systems = section(
        System.objects.filter(mine).select_related("vendor")
        # publication_state walks both, so fetch them once for the rows on this page.
        .prefetch_related("submissions", "test_runs").distinct()
        .order_by("vendor__name", "name"),
        "systems", search=("name", "vendor__name"),
    )
    components = section(
        Component.objects.filter(mine).select_related("vendor")
        .prefetch_related("submissions", "test_runs").distinct()
        .order_by("kind", "vendor__name", "name"),
        "components", search=("name", "vendor__name"),
    )
    software = section(
        Software.objects.filter(
            models.Q(created_by=user) | models.Q(submissions__submitter=user)
        ).select_related("vendor").prefetch_related("compatibility__release").distinct()
        .order_by("vendor__name", "name"),
        "software", search=("name", "vendor__name"),
    )

    # A hardware submission's own state, hung on the listing it is about, exactly as
    # ``latest_submission`` is for software below.
    #
    # This page queried listings and never ``hardware.Submission`` at all, so a submitter saw
    # their listing and nothing about what had happened to it. A reviewer who sent one back with
    # notes reached nobody: no email, no status, and ``reviewer_notes`` appeared in no
    # submitter-facing template.
    #
    # Only the listings on this page, and ascending so that for a listing submitted more than once
    # the newest row survives in the dict.
    listings = [*systems.page.object_list, *components.page.object_list]
    hardware_submissions = {}
    for submission in (
        HardwareSubmission.objects.filter(submitter=user)
        .prefetch_related("cited_releases").order_by("submitted_at")
    ):
        listing = submission.listing
        if listing is not None:
            hardware_submissions[(type(listing).__name__, listing.pk)] = submission
    for listing in listings:
        listing.user_can_edit = can_edit_listing(user, listing)
        listing.latest_submission = hardware_submissions.get(
            (type(listing).__name__, listing.pk))
        # Why an unpublished row is unpublished, and what would change it. "Unpublished" on its own
        # is a fact about the column rather than an answer, and for a seeded CPU or GPU family that
        # a run was merely classified against the answer is that it is not the submitter's listing.
        listing.publication = publication_state(listing, user)

    # One row per product carrying its own submission state, rather than a second table of
    # submissions beside it: a submission is only ever *about* a product, so two tables asked the
    # reader to join them by name to answer "what is happening with this listing".
    latest_submissions = {
        s.software_id: s
        for s in SoftwareSubmission.objects.filter(submitter=user).order_by("submitted_at")
    }
    for product in software.page.object_list:
        product.user_can_edit = can_edit_listing(user, product)
        product.latest_submission = latest_submissions.get(product.pk)

    runs = TestRun.objects.filter(submitter=user).select_related(
        "alma_release", "listing_system", "listing_system__vendor")
    # Four querysets rather than one list split in Python. The split read every run the user had
    # ever submitted into memory, with its joins, to display two hundred of them.
    validation = runs.exclude(run_type=RunType.benchmark.value)
    benchmark = runs.filter(run_type=RunType.benchmark.value)
    run_search = ("system_vendor", "system_product", "board_vendor", "board_model", "cpu_model")

    def runs_section(queryset, key, *, archived):
        return section(
            queryset.filter(archived_at__isnull=not archived).order_by("-received_at"),
            key, search=run_search,
        )

    sections = {
        "systems": systems,
        "components": components,
        "software": software,
        "validation": runs_section(validation, "validation", archived=False),
        "validation_archived": runs_section(validation, "validation_archived", archived=True),
        "benchmark": runs_section(benchmark, "benchmark", archived=False),
        "benchmark_archived": runs_section(benchmark, "benchmark_archived", archived=True),
        # Bounded like everything else. A person with one membership never notices; a distributor
        # with a hundred was rendering all of them, each fetching its vendor separately.
        "memberships": section(
            VendorMembership.objects.filter(user=user).select_related("vendor")
            .order_by("vendor__name"),
            "memberships", search=("vendor__name",),
        ),
        "claims": section(
            VendorClaim.objects.filter(requester=user, status__in=VendorClaim.OPEN_STATUSES)
            .select_related("vendor").order_by("-submitted_at"),
            "claims", search=("vendor__name",),
        ),
    }

    return render(
        request,
        "accounts/dashboard.html",
        {
            "sections": sections,
            # A deep link into an archived page has to land on the archived tab, or the pager
            # sends the reader to a page of a list the page is not showing.
            "validation_pane_archived": _pane_is_archived(request, "validation"),
            "benchmark_pane_archived": _pane_is_archived(request, "benchmark"),
            "pending_actions": _pending_actions(user),
        },
    )


def _pane_is_archived(request, kind: str) -> bool:
    """Whether the archived half of a run card is the one being addressed."""
    return any(request.GET.get(f"{prefix}_{kind}_archived")
               for prefix in ("page", "q"))


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
    """Enter the code shown by ``alma-certify register`` and approve the machine.

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
    elif request.method == "GET" and request.GET.get("code"):
        # Arrived from a scanned QR (verification_uri_complete carries ?code=). Resolve it and show
        # the approval screen straight away, so the operator only clicks Authorize - no retyping.
        # Read-only: approval is still the explicit POST to activate_confirm, and the client/IP are
        # shown first, so the two-step safeguard holds. A bad code here is a page load, not a guess,
        # so it is not counted toward the lockout - it just falls through to the manual form.
        probe = ActivateForm({"user_code": request.GET["code"]})
        if probe.is_valid():
            device_request = probe.find_request()
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
def account_settings(request: HttpRequest) -> HttpResponse:
    """Account-wide publishing preferences.

    The anonymity preference is the default applied to runs at ingest, not a live
    override of what is already published - a submitter can attribute or anonymize any
    single run from their dashboard, and a preference that silently overruled those
    choices every time it was read would take that back. Applying it to existing runs is
    therefore an explicit, separate tick on this form.
    """
    # Locally imported like the other results-side calls in this module, which keeps the
    # accounts app free of an import-time dependency on results.
    from lumina.results import services

    prefs = AccountSettings.for_user(request.user)
    form = AccountSettingsForm(request.POST or None, instance=prefs)
    if request.method == "POST" and form.is_valid():
        prefs = form.save()
        log_action("account_settings.update", target=prefs, actor=request.user,
                   after={"publish_anonymously": prefs.publish_anonymously})
        updated = 0
        if form.cleaned_data.get("apply_to_existing"):
            updated = services.apply_anonymity_to_runs(
                request.user, anonymous=prefs.publish_anonymously
            )
        messages.success(
            request,
            "Saved. New runs will be listed as “Anonymous”."
            if prefs.publish_anonymously
            else "Saved. New runs will be listed under your username.",
        )
        if updated:
            messages.info(
                request,
                f"{updated} existing run(s) updated to match.",
            )
        return redirect("accounts:settings")
    # Which endpoints could actually deliver a direct message, named as an admin named
    # them, so "Mattermost" is not an abstraction the reader has to trust: they can see
    # the workspace it will arrive from, and the absence of any tells them the option is
    # not going to do anything yet.
    from lumina.notifications.models import NotificationEndpoint, PolicyDestination

    chat_endpoints = sorted({
        destination.endpoint.name
        for destination in PolicyDestination.objects.filter(
            enabled=True, kind=PolicyDestination.KIND_PERSON,
            endpoint__kind=NotificationEndpoint.KIND_MATTERMOST,
            policy__enabled=True, endpoint__enabled=True,
        ).select_related("endpoint")
    })
    return render(request, "accounts/settings.html", {
        "form": form,
        "chat_endpoints": chat_endpoints,
    })


@login_required
def tokens(request: HttpRequest) -> HttpResponse:
    form = ApiTokenCreateForm(request.POST or None, user=request.user)
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
