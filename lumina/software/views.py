"""Public software catalog pages.

The detail page's per-major table is the point of the whole subsystem: one row
per cited AlmaLinux major, each with its own tier and its own community
confirmation count, so a vendor who certified once and walked away cannot leave a
listing that reads as currently certified.

Confirming that a product works is one POST with nothing but a CSRF token, and
returns the swapped row when HTMX asks so the count moves in place. Reporting a
major the listing does not cite is the one action that goes to review, because it
adds a major to somebody else's listing.
"""
from __future__ import annotations

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.mail import send_mail
from django.db.models import Count, Q
from django.http import (
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseForbidden,
    HttpResponseRedirect,
)
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from lumina.core.http import params, redirect_preserving_query, vendor_facet_context
from lumina.releases.models import AlmaLinuxRelease
from lumina.software import services
from lumina.software.filters import filter_software
from lumina.software.forms import SoftwareEditProposalForm, SoftwareSubmissionForm
from lumina.software.models import (
    Software,
    SoftwareCompatibility,
    SoftwareSubmission,
)
from lumina.taxonomy.models import Category
from lumina.vendors.services import (
    can_edit_listing,
    is_claimable,
)


def _visible_compatibility(software: Software, user):
    """Cited majors this viewer may see, newest first.

    A community-reported major is hidden until a reviewer accepts it, except from
    the person who reported it - who would otherwise click "report" and see
    nothing happen. Same spirit as ``TestRunQuerySet.visible_to``.
    """
    rows = software.compatibility.select_related("release").annotate(
        confirmations=Count("attestations", distinct=True),
    )
    visible = Q(status=SoftwareCompatibility.STATUS_APPROVED)
    if getattr(user, "is_authenticated", False):
        visible |= Q(proposed_by=user)
    return rows.filter(visible)


def _vendor_facet_context(request: HttpRequest) -> dict:
    return vendor_facet_context(
        request, Software, search_url=reverse("software:vendor_search"),
    )


def vendor_search(request: HttpRequest) -> HttpResponse:
    """Re-render just the vendor checkboxes for a search term.

    Server-side on purpose: the rendered list is a window, so filtering it in the
    browser would report "no such vendor" for anything outside the window.

    A request that is *not* from HTMX is bounced to the catalog rather than served
    the fragment. The search box sits inside the filter form, so a browser could
    arrive here by submitting it, and answering with a partial paints the options
    list - "No vendor matches that name" and all - as the entire document. The
    query string is carried over so the page lands with the same filters and the
    same term still in the box.
    """
    if request.headers.get("HX-Request") != "true":
        return redirect_preserving_query(request, "software:browse")
    return render(
        request,
        "catalog/_vendor_options.html",
        {**_vendor_facet_context(request), "active_filters": params(request)},
    )


def browse(request: HttpRequest) -> HttpResponse:
    """The software catalog, with the same HTMX partial pattern as hardware:
    one view, two templates, chosen on the request header."""
    listings = (
        filter_software(params=params(request))
        .select_related("vendor")
        # Only the category chips need a prefetch; the card's badge comes from
        # ``validation_level``, already on the row.
        .prefetch_related("category_values__value")
        # Totalled here rather than stored on Software, whose counts are per major
        # by design. Approved majors only: a community-reported release is hidden
        # until a reviewer accepts it, so its confirmations must not show up in a
        # public total either.
        .annotate(
            attestation_count=Count(
                "compatibility__attestations",
                distinct=True,
                filter=Q(
                    compatibility__status=SoftwareCompatibility.STATUS_APPROVED
                ),
            )
        )
    )
    categories = list(
        Category.objects.filter(applies_to=Category.APPLIES_SOFTWARE)
        .prefetch_related("values")
    )
    context = {
        "listings": listings,
        "categories": categories,
        **_vendor_facet_context(request),
        "releases": AlmaLinuxRelease.objects.supported(),
        "active_filters": params(request),
        # Consumed by the shared filter panel, which also serves hardware's
        # component-kind block.
        "kind": "software",
    }
    template = (
        "software/_results.html"
        if request.headers.get("HX-Request") == "true"
        else "software/browse.html"
    )
    return render(request, template, context)


def detail(request: HttpRequest, slug: str) -> HttpResponse:
    software = (
        Software.objects.filter(slug=slug, published=True)
        .select_related("vendor", "owner_vendor")
        .first()
    )
    if software is None:
        raise Http404("No published software with this slug.")

    compatibility = list(_visible_compatibility(software, request.user))
    cited = {row.release_id for row in software.compatibility.all()}
    return render(
        request,
        "software/detail.html",
        {
            "software": software,
            "compatibility": compatibility,
            "user_can_edit": can_edit_listing(request.user, software),
            # The doorway for a real vendor to take an unowned listing over,
            # offered only while the identity is both unowned and unverified.
            "vendor_claimable": is_claimable(software.vendor),
            # Bounded by what the Foundation has released, minus what is already
            # cited, so the control cannot create a duplicate row or invent a
            # release.
            "reportable_releases": AlmaLinuxRelease.objects.supported().exclude(
                pk__in=cited
            ),
            "confirmed_majors": _confirmed_majors(software, request.user),
            # Passed rather than hardcoded in the shared card, which serves both
            # catalogs. The fourth column is software's confirm/withdraw control,
            # which hardware has no equivalent of - attesting to hardware means
            # running the suite.
            "software_compatibility_columns": [
                "Release", "Validated by", "Community", "",
            ],
            "software_compatibility_note": (
                "A release validated by its vendor or by AlmaLinux still shows how "
                "many community members have independently confirmed it. The badge "
                "at the top of the page is the highest level across all releases, "
                "so check this table for the release you care about."
            ),
        },
    )


def _confirmed_majors(software: Software, user) -> set[int]:
    """Majors this viewer has already confirmed, so the row offers Withdraw."""
    if not getattr(user, "is_authenticated", False):
        return set()
    return set(
        SoftwareCompatibility.objects.filter(
            software=software, attestations__user=user
        ).values_list("release__major", flat=True)
    )


def _row_response(request: HttpRequest, software: Software, major: int) -> HttpResponse:
    """Re-render one table row for HTMX, or bounce back to the page without it."""
    if request.headers.get("HX-Request") == "true":
        row = _visible_compatibility(software, request.user).filter(
            release__major=major
        ).first()
        return render(
            request,
            "software/_compatibility_row.html",
            {
                "software": software,
                "row": row,
                "confirmed_majors": _confirmed_majors(software, request.user),
                "user_can_edit": can_edit_listing(request.user, software),
            },
        )
    return HttpResponseRedirect(software.get_absolute_url())


@login_required
@require_POST
def attest(request: HttpRequest, slug: str, major: int) -> HttpResponse:
    """One click: "yes, this works". No detail asked, no review queue entry."""
    software = get_object_or_404(Software, slug=slug, published=True)
    release = get_object_or_404(AlmaLinuxRelease, major=major)
    try:
        services.attest(software=software, release=release, user=request.user)
    except ValueError as exc:
        messages.info(request, str(exc))
        return HttpResponseRedirect(software.get_absolute_url())
    return _row_response(request, software, major)


@login_required
@require_POST
def withdraw(request: HttpRequest, slug: str, major: int) -> HttpResponse:
    software = get_object_or_404(Software, slug=slug, published=True)
    release = get_object_or_404(AlmaLinuxRelease, major=major)
    try:
        services.withdraw_attestation(
            software=software, release=release, user=request.user
        )
    except ValueError as exc:
        messages.info(request, str(exc))
    return _row_response(request, software, major)


@login_required
@require_POST
def report_major(request: HttpRequest, slug: str) -> HttpResponse:
    """Cite a major the listing does not have yet.

    The one control here that is not a single click, because it makes a claim
    about somebody else's listing rather than agreeing with one already on it.
    """
    software = get_object_or_404(Software, slug=slug, published=True)
    try:
        major = int(request.POST.get("release", ""))
    except ValueError:
        messages.error(request, "Pick an AlmaLinux release.")
        return HttpResponseRedirect(software.get_absolute_url())
    release = get_object_or_404(AlmaLinuxRelease, major=major)
    try:
        services.report_new_major(
            software=software, release=release, user=request.user
        )
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request,
            f"Thanks. A reviewer will check that {software.name} works on "
            f"AlmaLinux {major} before it appears publicly.",
        )
    return HttpResponseRedirect(software.get_absolute_url())


@login_required
def propose_edit(request: HttpRequest, slug: str) -> HttpResponse:
    """Propose a correction to a listing this user's vendor maintains.

    Permission comes from the shared ``can_edit_listing``, which only reads
    ``owner_vendor`` - so a community listing nobody has claimed is admin-only,
    exactly as on the hardware side.
    """
    software = get_object_or_404(Software, slug=slug)
    if not can_edit_listing(request.user, software):
        return HttpResponseForbidden(
            "You need a submit-role membership in this listing's owner vendor to "
            "propose edits."
        )
    form = SoftwareEditProposalForm(request.POST or None, software=software)
    if request.method == "POST" and form.is_valid():
        proposal = form.save(commit=False)
        proposal.software = software
        proposal.proposed_by = request.user
        proposal.save()
        messages.success(
            request, f"Edit proposal for {software.name} submitted for review."
        )
        return HttpResponseRedirect(software.get_absolute_url())
    return render(
        request, "software/propose_edit.html", {"form": form, "software": software}
    )


@login_required
def submit(request: HttpRequest) -> HttpResponse:
    """Submit a software product for certification review."""
    form = SoftwareSubmissionForm(
        request.POST or None, request.FILES or None, user=request.user,
    )
    if request.method == "POST" and form.is_valid():
        submission = form.save()
        _notify_reviewers(submission)
        messages.success(
            request,
            f"{submission.software.name} submitted for review. It appears in the "
            "catalog once a reviewer approves it.",
        )
        return HttpResponseRedirect(reverse("accounts:dashboard"))
    return render(request, "software/submit.html", {"form": form})


@login_required
def revise(request: HttpRequest, uuid: str) -> HttpResponse:
    """Fix a submission a reviewer sent back, and put it back in the queue.

    Restricted to the submitter's own needs-changes submissions. Anything else is
    a 404 rather than a 403: the set of submissions a user may revise is private,
    so "wrong status" and "not yours" should be indistinguishable from outside.

    The draft listing is edited in place and the same submission row returns to
    pending. This is deliberately not ``propose_edit``, which produces a
    ``SoftwareEditProposal`` against a *published* listing and needs
    ``owner_vendor`` rights the original submitter usually does not have.
    """
    submission = get_object_or_404(
        SoftwareSubmission.objects.select_related("software", "software__vendor"),
        uuid=uuid,
        submitter=request.user,
        status=SoftwareSubmission.STATUS_NEEDS_CHANGES,
    )
    form = SoftwareSubmissionForm(
        request.POST or None, request.FILES or None,
        user=request.user, submission=submission,
    )
    if request.method == "POST" and form.is_valid():
        form.save()
        _notify_reviewers(submission)
        messages.success(
            request,
            f"{submission.software.name} resubmitted. A reviewer will take "
            "another look.",
        )
        return HttpResponseRedirect(reverse("accounts:dashboard"))
    return render(
        request,
        "software/revise.html",
        {"form": form, "submission": submission},
    )


def _notify_reviewers(submission) -> None:
    """Best-effort mail, mirroring ``hardware.submit_views``. No-op when the
    recipient list is empty, which is the devstack default."""
    recipients = settings.LUMINA_REVIEW_NOTIFY_EMAILS
    if not recipients:
        return
    send_mail(
        subject=f"[lumina] Software submission: {submission.software.name}",
        message=(
            f"{submission.submitter} submitted {submission.software.name} "
            f"claiming {submission.get_claimed_validation_level_display()}."
        ),
        from_email=None,
        recipient_list=list(recipients),
        fail_silently=True,
    )
