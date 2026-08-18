"""Reviewer views for certification-suite runs.

Validation/collect runs and benchmark runs are separate queues (separate tabs
in the dashboard): validation runs are certification evidence, benchmark runs
feed the public leaderboards and are reviewed for plausibility.
"""
from __future__ import annotations

from django.contrib import messages
from django.db import transaction
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from lumina.audit.services import log_action
from lumina.results import services
from lumina.results.forms import RunComponentTiesForm, RunListingAssignForm
from lumina.results.models import ResultStatus, Severity, TestRun
from lumina.review.permissions import reviewer_required


@reviewer_required
def run_detail(request: HttpRequest, pk: int) -> HttpResponse:
    run = get_object_or_404(
        TestRun.objects.select_related(
            "submitter", "alma_release", "listing_system", "listing_system__vendor"
        ),
        pk=pk,
    )
    assign_form = RunListingAssignForm(
        run=run,
        initial={
            "system": run.listing_system,
            "components": run.listing_components.all(),
            "claimed_validation_level": run.claimed_validation_level,
            "available_from_minor": run.available_from_minor,
            "pre_release": run.pre_release,
            "publish_requested_date": run.publish_requested_date,
        }
    )
    return render(
        request,
        "review/run_detail.html",
        {
            "run": run,
            "results": run.results.all(),
            "benchmarks": run.benchmarks.all(),
            "artifacts": run.artifacts.all(),
            "counts": run.status_counts(),
            "verdict": run.verdict(),
            # Exactly what verdict() gates on, so a reviewer can see why
            # approving this run will not certify anything.
            "blocking_results": run.results.exclude(
                severity=Severity.INFORMATIONAL
            ).filter(status__in=[ResultStatus.FAIL, ResultStatus.ERROR]),
            "assign_form": assign_form,
            # The machine's other queued runs. A vendor validates one machine on
            # several releases, so these differ from this run only in which
            # release they passed on - offering them together saves opening each.
            "pending_siblings": _pending_siblings(run),
            # What approving would do to the catalog, worded from what it will actually
            # use. The box this feeds printed the proposal blob directly and promised a
            # listing would be created, both wrong on a run against hardware already
            # listed.
            "effect": services.proposal_effect(run),
            # One list carrying both the status and the controls. There were two - a grouped
            # read-only preview and a flat editable form over the same entries - so the same
            # part appeared twice on one page. ``component_groups`` groups the form's own rows
            # through the same helper the preview used.
            "component_form": RunComponentTiesForm(run=run),
        },
    )


def _pending_siblings(run: TestRun) -> list:
    """This machine's other queued runs, each annotated with whether it passed.

    A reviewer needs to see up front which ones the group button will take and
    which it will leave behind, rather than finding out from the result.
    """
    if run.status != TestRun.STATUS_PENDING:
        return []
    siblings = list(services.pending_sibling_runs(run))
    for sibling in siblings:
        sibling.passed = sibling.verdict()
    return siblings


@reviewer_required
@require_POST
def run_assign_listing(request: HttpRequest, pk: int) -> HttpResponse:
    run = get_object_or_404(TestRun, pk=pk)
    # ``run=`` on the bound form too, not just the one the page renders. Without it the form cannot
    # apply any rule that depends on the run: it rebuilt every field including the System picker, so
    # a scoped run's POST still carried a System and was only stopped by ``assign_listing`` raising.
    form = RunListingAssignForm(request.POST, run=run)
    if form.is_valid():
        services.assign_listing(
            run,
            # ``.get`` because the fields are removed outright for a scoped run, so they are absent
            # from ``cleaned_data`` rather than blank.
            system=form.cleaned_data.get("system"),
            components=form.cleaned_data["components"],
            level=form.cleaned_data.get("claimed_validation_level", ""),
            machine_kind=form.cleaned_data.get("machine_kind", ""),
            available_from_minor=form.cleaned_data.get("available_from_minor"),
            # These fields are always on this form, so a blank box is a decision to clear rather
            # than silence. That is what lets a reviewer end a hold as well as start one.
            set_available_from_minor=True,
            pre_release=form.cleaned_data.get("pre_release", False),
            publish_requested_date=form.cleaned_data.get("publish_requested_date"),
            set_embargo=True,
            by=request.user,
        )
        messages.success(request, "Listing assignment updated.")
    else:
        messages.error(request, "Could not update the assignment.")
    return HttpResponseRedirect(reverse("review:run_detail", args=[run.pk]))


def _save_component_ties(request: HttpRequest, run: TestRun, *, commit: bool = True) -> bool:
    """Persist the component answers carried in this POST. True if they were saved.

    ``commit=False`` validates and reports without writing, so a caller can refuse before it has
    changed anything.

    Shared by the save button and both approve buttons, which are controls of the same form (see
    ``#run-review`` in the template) precisely so that approving cannot proceed on a different set
    of answers from the ones on screen.

    A request that never rendered the section posts no marker, and the form then carries the
    stored answers forward rather than reading the silence as "untick everything" - so calling
    this on every approval is safe for the API and for any page without the controls.
    """
    form = RunComponentTiesForm(request.POST, run=run)
    if not form.is_valid():
        return False
    if not commit:
        return True
    run.excluded_component_ties = form.excluded_tie_keys()
    run.component_overrides = form.component_overrides()
    run.save(update_fields=["excluded_component_ties", "component_overrides"])
    log_action(
        "test_run.component_ties_edit", target=run, actor=request.user,
        after={"excluded": run.excluded_component_ties,
               "overrides": run.component_overrides},
    )
    return True


@reviewer_required
@require_POST
def run_component_ties(request: HttpRequest, pk: int) -> HttpResponse:
    """Keep, drop, or correct the components a run would tie on approval.

    The reviewer's half of what the submitter can already do. Their preview listed the parts
    read-only, so a reviewer who could see that DMI reported "OEM" for a whitebox board had
    no way to fix it before approval created a catalog manufacturer named OEM.
    """
    run = get_object_or_404(TestRun, pk=pk)
    if _save_component_ties(request, run):
        messages.success(request, "Component ties updated.")
    else:
        messages.error(request, "Could not update the component ties.")
    return HttpResponseRedirect(reverse("review:run_detail", args=[run.pk]))


@reviewer_required
@require_POST
def run_approve(request: HttpRequest, pk: int) -> HttpResponse:
    run = get_object_or_404(TestRun, pk=pk)
    # The component answers arrive with the approval, because the controls and this button are
    # controls of one form. Saved first, so ``approve_run`` reads what the reviewer decided rather
    # than what was stored the last time somebody remembered to press a second button.
    #
    # A form that will not validate stops the approval. Approving anyway would certify on answers
    # nobody can see, and the tier those answers set is not recoverable afterwards from the page.
    if not _save_component_ties(request, run, commit=False):
        messages.error(
            request,
            "The component answers on this page could not be read, so nothing was approved. "
            "Reload the run and try again.",
        )
        return HttpResponseRedirect(reverse("review:run_detail", args=[run.pk]))
    try:
        # One transaction, so a refused approval leaves nothing behind. ``approve_run`` turns a
        # run down for several reasons a reviewer meets in the ordinary course - the wrong status,
        # an unreleased OS gate, a machine nothing identifies - and the answers were being written
        # anyway. A rejected decision that silently changed the run's data is the worst of both:
        # the reviewer is told nothing happened and something did.
        with transaction.atomic():
            _save_component_ties(request, run)
            services.approve_run(run, by=request.user, notes=request.POST.get("notes", ""))
    except services.ReviewError as exc:
        messages.error(request, str(exc))
        return HttpResponseRedirect(reverse("review:run_detail", args=[run.pk]))
    if run.is_embargoed:
        # A hold with no date is a hold until somebody lifts it, and this said
        # "Embargoed until None". The dateless case is the ordinary one for unreleased hardware
        # whose announcement date is not settled, so it is the message a reviewer sees most.
        if run.publish_requested_date:
            messages.success(
                request,
                f"Approved. Embargoed until {run.publish_requested_date} - "
                "invisible to the public until then.",
            )
        else:
            messages.success(
                request,
                "Approved and held. No release date was given, so it stays invisible to the "
                "public until somebody sets one or clears the hold.",
            )
    elif run.verdict() is False:
        # verdict() is False only for a validate run that failed. Saying
        # "approved and published" here is true of the run and false of
        # everything the reviewer was trying to achieve: no listing is
        # certified, no components are tied, nothing reaches the catalog.
        messages.warning(
            request,
            "Approved and published as a record, but this run did not pass, so "
            "no listing was certified, no components were tied, and nothing "
            "from it appears in the public catalog. Fix the failures and "
            "submit a new run to certify this hardware.",
        )
    else:
        messages.success(request, "Approved and published.")
    return HttpResponseRedirect(reverse("review:queue"))


@reviewer_required
@require_POST
def run_approve_group(request: HttpRequest, pk: int) -> HttpResponse:
    """Approve this run and the machine's other queued runs in one action."""
    run = get_object_or_404(TestRun, pk=pk)
    if not _save_component_ties(request, run, commit=False):
        messages.error(
            request,
            "The component answers on this page could not be read, so nothing was approved. "
            "Reload the run and try again.",
        )
        return HttpResponseRedirect(reverse("review:run_detail", args=[run.pk]))
    # No blanket rollback here, unlike the single approval. ``approve_group`` is a partial action
    # by design: it reports what it approved and what it left behind, and wrapping the lot would
    # throw away good approvals because one sibling was refused. Each member's own answers are
    # written next to its own approval inside the loop.
    _save_component_ties(request, run)
    approved, blocked = services.approve_group(
        run, by=request.user, actor=request.user,
        notes=request.POST.get("notes", ""),
    )
    if approved:
        releases = ", ".join(str(member.alma_release) for member in approved)
        embargoed = [member for member in approved if member.is_embargoed]
        message = (
            f"Approved {len(approved)} runs of {approved[0].display_name}: "
            f"{releases}."
        )
        if embargoed:
            message += (
                f" {len(embargoed)} embargoed and invisible publicly until "
                "the requested date."
            )
        messages.success(request, message)
    for member, reason in blocked:
        # Named individually, because "2 runs were skipped" leaves the reviewer
        # to go find which two and why.
        messages.warning(
            request, f"{member.alma_release} was not approved: {reason}."
        )
    if not approved:
        return HttpResponseRedirect(reverse("review:run_detail", args=[run.pk]))
    return HttpResponseRedirect(reverse("review:queue"))


@reviewer_required
@require_POST
def run_reject(request: HttpRequest, pk: int) -> HttpResponse:
    run = get_object_or_404(TestRun, pk=pk)
    try:
        services.reject_run(run, by=request.user, reason=request.POST.get("reason", ""))
    except services.ReviewError as exc:
        messages.error(request, str(exc))
        return HttpResponseRedirect(reverse("review:run_detail", args=[run.pk]))
    messages.info(request, "Run rejected.")
    return HttpResponseRedirect(reverse("review:queue"))


@reviewer_required
@require_POST
def run_release_quarantine(request: HttpRequest, pk: int) -> HttpResponse:
    """Override the OS gate on one run.

    Deliberately not an "approve anyway" button: it returns the run to the normal
    queue, where it still has to be reviewed on its merits. The reason is required
    by the service, so the redirect below is the ordinary path for a reviewer who
    submitted the form empty.
    """
    run = get_object_or_404(TestRun, pk=pk)
    try:
        services.release_from_quarantine(
            run, by=request.user, reason=request.POST.get("reason", "")
        )
    except services.ReviewError as exc:
        messages.error(request, str(exc))
        return HttpResponseRedirect(reverse("review:run_detail", args=[run.pk]))
    # Where it went, not where it usually goes. ``normal_initial_status`` sends a validate run
    # back to the submitter as a draft, because it still has no listing details; only a benchmark
    # run rejoins the queue. Telling a reviewer to expect it in their queue sent them looking for
    # something that was never going to be there.
    run.refresh_from_db()
    if run.status == TestRun.STATUS_DRAFT:
        messages.info(
            request,
            "Released from quarantine. The run is back with its submitter as a draft, so they "
            "can supply the listing details and submit it for review.",
        )
    else:
        messages.info(
            request,
            "Released from quarantine. The run is back in the review queue and still "
            "needs reviewing on its merits.",
        )
    return HttpResponseRedirect(reverse("review:run_detail", args=[run.pk]))


@reviewer_required
@require_POST
def run_request_changes(request: HttpRequest, pk: int) -> HttpResponse:
    run = get_object_or_404(TestRun, pk=pk)
    try:
        services.request_run_changes(
            run, by=request.user, reason=request.POST.get("reason", "")
        )
    except services.ReviewError as exc:
        messages.error(request, str(exc))
        return HttpResponseRedirect(reverse("review:run_detail", args=[run.pk]))
    messages.info(request, "Changes requested.")
    return HttpResponseRedirect(reverse("review:queue"))
