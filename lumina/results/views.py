"""Public result pages: upload, run detail, leaderboards, statistics."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from lumina.audit.services import log_action
from lumina.results import compare, gpu_metrics, ingest, services
from lumina.results import filters as result_filters
from lumina.results.forms import BundleUploadForm, RunListingProposalForm
from lumina.results.highlights import (
    attach_headlines,
    benchmark_label,
    category_label,
)
from lumina.results.models import RunType, TestRun


@login_required
def upload(request: HttpRequest) -> HttpResponse:
    """Manual bundle upload - the offline half of the submission story."""
    form = BundleUploadForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            run = ingest.ingest_bundle(
                submitter=request.user,
                bundle_file=form.cleaned_data["bundle"],
                source="web_upload",
                pre_release=form.cleaned_data["pre_release"] or None,
                publish_after=form.cleaned_data["publish_after"],
                submitter_notes=form.cleaned_data["notes"],
            )
        except ingest.DuplicateRun as exc:
            if exc.identical:
                messages.info(request, "This run was already submitted.")
                return redirect(exc.run.get_absolute_url())
            form.add_error("bundle", exc.detail)
        except ingest.BundleError as exc:
            form.add_error("bundle", exc.detail)
        else:
            messages.success(
                request,
                "Results received. They will appear publicly once reviewed"
                + (" and the embargo lifts." if run.pre_release else "."),
            )
            return redirect(run.get_absolute_url())
    return render(request, "results/upload.html", {"form": form})


def run_detail(request: HttpRequest, uuid) -> HttpResponse:
    run = (
        TestRun.objects.visible_to(request.user)
        .select_related("alma_release", "listing_system", "listing_system__vendor",
                        "submitter")
        .filter(uuid=uuid)
        .first()
    )
    if run is None:
        raise Http404
    return render(
        request,
        "results/run_detail.html",
        {
            "run": run,
            "results": run.results.all(),
            # Ordered so the GPU figures read API by API and category by category, rather than
            # alphabetically by metric name, which interleaved bandwidth with compute.
            "benchmarks": gpu_metrics.reading_order(run.benchmarks.all()),
            "counts": run.status_counts(),
            "verdict": run.verdict(),
            # Ordered by kind so the board, CPU, and GPUs group together, and
            # select_related because each row renders its vendor.
            "linked_components": run.listing_components.select_related(
                "vendor"
            ).order_by("kind", "name"),
            "sibling_drafts": (
                list(services.sibling_draft_runs(run))
                if run.submitter_id == request.user.id else []
            ),
            "listing_prompt": _listing_prompt(request.user, run),
            "is_draft": run.status == TestRun.STATUS_DRAFT,
            "needs_changes": run.status == TestRun.STATUS_NEEDS_CHANGES,
            # A run the reviewer sent back is the submitter's to act on again,
            # so it gets the same edit-and-submit controls as a fresh draft.
            "can_resubmit": (
                run.submitter_id == request.user.id
                and run.status in services.SUBMITTABLE_STATUSES
            ),
            "outstanding": (
                _outstanding_details(run) if run.submitter_id == request.user.id else []
            ),
        },
    )


def _outstanding_details(run) -> list:
    from lumina.results.services import missing_submission_details

    return missing_submission_details(run)


def _subject_for(run) -> str:
    """What the form is describing: a component, a system, a motherboard, or an unnamed box.

    Honors the submitter's own correction, so someone who has said "this is a
    vendor system" is asked for a system name rather than a board name.
    """
    from lumina.results.inventory_extract import is_placeholder
    from lumina.results.models import SystemKind

    # A scoped run is about its component, whatever chassis it happens to be in. Checked first
    # because the host's DMI is still fully populated on a scoped run and would otherwise answer
    # this question with the wrong subject every time.
    if run.is_scoped:
        return "component"
    if run.effective_system_kind == SystemKind.PREBUILT:
        return "system"
    # A custom build is identified by its board - unless the firmware named no board either, and
    # then nothing identifies the machine and every field has to come from the submitter.
    #
    # Keyed on the identity rather than on a third kind. There used to be a "unknown"
    # ``SystemKind`` and this returned "machine" for it; with two kinds and custom as the
    # fallback, the question "is this board usable as an identity?" is the one that was really
    # being asked.
    if is_placeholder(run.board_vendor) or is_placeholder(run.board_model):
        return "machine"
    return "motherboard"


def _listing_prompt(user, run) -> dict | None:
    """What (if anything) to offer the submitter about the catalog.

    - New model -> offer the propose-a-listing form.
    - Already cataloged and the user maintains that listing -> point at the
      existing edit-proposal flow.
    - Already cataloged, not the maintainer -> nothing to do; stay quiet.
    """
    from lumina.results.models import RunType
    from lumina.results.services import find_matching_board, find_matching_system
    from lumina.vendors.services import can_edit_listing

    if (
        not getattr(user, "is_authenticated", False)
        or user != run.submitter
        or run.run_type != RunType.validate.value
        or run.status == TestRun.STATUS_REJECTED
    ):
        return None

    # Every kind of machine gets a prompt. Restricting this to prebuilt left
    # custom and unidentified machines with no form at all, which is why they
    # sailed through with nothing filled in.
    # ``_subject_for`` decides which of the three things this run is about, including the
    # machine-nothing-identifies case that used to be a third ``SystemKind``. One implementation,
    # so the prompt on the run page and the copy on the form cannot disagree about what is being
    # asked for - they did while each derived it separately.
    subject = _subject_for(run)
    if subject == "component":
        # Nothing to offer and nothing to ask. The parts a scoped run is evidence for are resolved
        # from its own inventory by ``ensure_component_ties`` at review, which is strictly better
        # than anything a submitter could type: a card is identified by its PCI IDs, not by a name
        # somebody remembers. Returning None keeps the machine prompt off the page, which is what
        # put "Add listing details" in front of a submitter whose run was about a GPU.
        return None
    if subject == "system":
        existing = run.listing_system or find_matching_system(
            run.system_vendor, run.system_product
        )
    elif subject == "motherboard":
        existing = find_matching_board(run.board_vendor, run.board_model)
    else:
        existing = None

    if existing is None:
        kind = "proposed" if run.listing_proposal else "propose"
        return {"kind": kind, "subject": subject}
    if can_edit_listing(user, existing):
        return {"kind": "owned", "system": existing, "subject": subject}
    return None


@login_required
def propose_listing(request: HttpRequest, uuid) -> HttpResponse:
    """Submitter fills in the catalog listing their validation run is for."""
    run = get_object_or_404(TestRun, uuid=uuid, submitter=request.user)
    if run.status not in services.SUBMITTABLE_STATUSES:
        messages.info(
            request,
            "This run has already been submitted and can no longer be edited.",
        )
        return HttpResponseRedirect(run.get_absolute_url())
    if run.run_type != RunType.validate.value:
        return HttpResponseRedirect(run.get_absolute_url())
    # Every field on this form describes the machine: its vendor, its displayed name, its model
    # number, and whether it is a vendor system or a custom build. A scoped run is not a claim about
    # the machine and can never carry a System listing, so there is no coherent answer to any of
    # them, and a saved one would be worse than none: ``listing_proposal`` feeds
    # ``effective_vendor`` and ``effective_product``, so filling this in would rename the run after
    # the host it is not about. Sent back with a reason rather than 404, because the submitter got
    # here from a button this page used to offer them.
    if run.is_scoped:
        kinds = " and ".join(run.scope_labels)
        messages.info(
            request,
            f"This run is evidence for its {kinds} only, so there are no machine details to add. "
            f"The {kinds} is identified from the run's own inventory when a reviewer approves it.",
        )
        return HttpResponseRedirect(run.get_absolute_url())
    # No gate on reaching this page, deliberately. It used to refuse anyone who did not speak
    # for the machine's vendor, which twice turned out to refuse the person who needed it: a
    # component vendor claiming their own part inside somebody else's chassis, and then the
    # submitter of a run the catalog matched to the wrong machine - the misidentified case is
    # precisely a submitter with no standing over the listing they were matched to.
    #
    # What a community member may not do is restate a listing, and that is enforced on the
    # fields (``_lock_identity``), not on the door. Everything else on this form - which
    # releases were validated, which parts to tie, their own notes - is theirs either way.
    subject = _subject_for(run)
    if request.method == "POST":
        # Taking the override back, before validation: an undo should not be blocked by an
        # invalid field somewhere else on the page, and it needs no field of its own.
        if "undo_identity_dispute" in request.POST and run.identity_disputed:
            run.identity_disputed = False
            run.save(update_fields=["identity_disputed"])
            log_action("test_run.identity_disputed", target=run, actor=request.user,
                       after={"disputed": False})
            messages.info(request, "Match restored.")
            return HttpResponseRedirect(
                reverse("results:propose_listing", args=[run.uuid])
            )
        # ``initial`` on a bound form too, which matters now that an already-claimed
        # release checkbox is disabled: Django reads a disabled field from ``initial``
        # and ignores whatever was posted for it, so without this every locked major
        # cleaned to False. That silently broke the *widening* half of the rule -
        # ``merge_listing_proposal`` takes the lower of the two floors only when the
        # major is present on both sides, so a submitter lowering 9.6 to 9.4 had their
        # correction dropped. Posted values still win for every enabled field.
        form = RunListingProposalForm(
            request.POST, initial=_proposal_initial(run), subject=subject, run=run,
            user=request.user,
        )
        if form.is_valid():
            # What the form says is listing data, rather than everything it cleaned minus a
            # list of names kept here. That list was short by one five times over, each key
            # found in a stray devstack row: ``attribution``, ``included_ties``,
            # ``tie_claim_*``, ``tie_edit_*``, ``components_submitted``. The form registers a
            # control field where it builds it, so the next one is stripped without anybody
            # remembering to come here.
            data = form.listing_proposal_data()
            cleaned = form.cleaned_data
            # These live on the run, not in the proposal: they decide what the evidence counts
            # as, and ``effective_level`` reads them straight off the run.
            #
            # The form owns the include-list-to-exclusion-set inversion; see
            # ``RunListingProposalForm.excluded_tie_keys``.
            run.excluded_component_ties = form.excluded_tie_keys()
            # Per-component corrections are about the parts, not about the listing being
            # described. The form resolves the boxes into only the ones that differ from the
            # report, and skips rows whose fields were locked to a catalog match.
            run.component_overrides = form.component_overrides()
            # "This is not that machine." A field of this form rather than its own endpoint,
            # so the identity the submitter just typed and the fact that they disowned the
            # match arrive in the same save - there is no order in which the fields are
            # editable but the flag is not yet set.
            #
            # One-way here. Clearing it is the undo button above, and reading a False out of
            # an unlocked form would silently re-link an already-disputed run: once the flag
            # is set the fields render as a normal card and the checkbox is not on the page.
            #
            # Clearing ``listing_system`` matters as much as setting the flag - ingest linked
            # it, and approval reuses a linked listing without consulting anything else.
            if cleaned.get("identity_disputed") and not run.identity_disputed:
                run.identity_disputed = True
                run.listing_system = None
                log_action("test_run.identity_disputed", target=run, actor=request.user,
                           after={"disputed": True})
            vendor_slug = cleaned.get("on_behalf_of") or ""
            level = cleaned.get("claimed_validation_level") or ""
            notes = cleaned.get("submitter_notes") or ""
            # The embargo, as reviewed here. Both values arrive at ingest from the CLI or the
            # upload form and were previously uncorrectable by the submitter: a missed flag, a
            # mistyped date, or hardware that stopped being unreleased between the run and the
            # submission all ended with the wrong thing happening publicly at approval.
            run.pre_release = bool(cleaned.get("pre_release"))
            run.publish_requested_date = cleaned.get("publish_requested_date")
            # Absent from the form entirely on a run that was not on Kitten, and ``cleaned`` has
            # no key for a field that does not exist - so read it defensively rather than
            # blanking a value a reviewer may have set.
            if "available_from_minor" in form.fields:
                run.available_from_minor = cleaned.get("available_from_minor")
            run.on_behalf_of = _vendor_by_slug(vendor_slug)
            run.claimed_validation_level = level
            run.submitter_notes = notes
            # Merged, not replaced. Re-saving this form used to retract AlmaLinux
            # support: unticking a box dropped a major, and a major that stopped
            # being supported() vanished from the form and so from the next save.
            run.listing_proposal = services.merge_listing_proposal(
                run.listing_proposal, data
            )
            run.save(update_fields=["listing_proposal", "on_behalf_of",
                                    "claimed_validation_level",
                                    "submitter_notes",
                                    "excluded_component_ties",
                                    "component_overrides",
                                    "identity_disputed", "listing_system",
                                    "pre_release", "publish_requested_date",
                                    "available_from_minor"])
            # The merged blob, not the posted one: the log should say what was
            # stored, and those differ now that releases accumulate.
            log_action("test_run.propose_listing", target=run,
                       actor=request.user,
                       after={**run.listing_proposal,
                              "on_behalf_of": vendor_slug,
                              "claimed_validation_level": run.claimed_validation_level})
            # One answer covers the machine, not just this run: a submitter
            # uploading several AlmaLinux versions back to back should not be
            # asked the same questions once per bundle.
            shared = services.share_listing_details(run)
            if shared:
                messages.info(
                    request,
                    f"These details were applied to your {len(shared)} other "
                    "unsubmitted run(s) of this machine "
                    f"({', '.join(str(r.alma_release or '?') for r in shared)}), "
                    "so you do not have to enter them again.",
                )
            messages.success(
                request,
                "Listing details saved. They will be reviewed together with "
                "the run, and the system is created on approval.",
            )
            return HttpResponseRedirect(run.get_absolute_url())
    else:
        form = RunListingProposalForm(
            initial=_proposal_initial(run), subject=subject, run=run,
            user=request.user,
        )
    return render(
        request, "results/propose_listing.html",
        {
            "run": run, "form": form, "subject": subject,
            # Read by submission-summary.js through json_script. Server-side because only
            # the server knows what the catalog holds today.
            "summary_baseline": services.submission_preview(run, request.user),
            # Named on the page when the identity fields are hidden, so a form about a
            # specific machine still says which machine.
            "linked_listing": services.existing_listing_for(run),
        },
    )


def _proposal_initial(run) -> dict:
    """Everything already known about the machine, as form initial data.

    Extracted so the bound and unbound branches of ``propose_listing`` cannot disagree.
    They used to: only the GET branch built this, which was harmless while every field
    was editable and became a bug the moment one was disabled.
    """
    initial = RunListingProposalForm.initial_from_run(run)
    if not run.listing_proposal:
        # Nothing answered here yet: borrow from another draft of the same machine if
        # the submitter has already done the work once.
        sibling = next(
            (s for s in services.sibling_draft_runs(run) if s.listing_proposal),
            None,
        )
        if sibling is not None:
            initial.update(sibling.listing_proposal)
    # A saved proposal wins over the DMI prefill, but only for the keys it actually
    # carries, so fields added since it was saved still prefill - and only where it
    # actually answers something.
    #
    # A stored blank is not an answer, and every consumer already reads the blob that way:
    # ``submitted_cpu_model`` is ``proposed or run.cpu_model``, and
    # ``create_listings_from_run`` does the same for the vendor, the name, and the model
    # number. This was a plain ``update``, so an empty string in the blob shadowed the value
    # the run itself reported. Reported as "why is the CPU model field empty still" on a run
    # whose DMI says ``Intel(R) Core(TM) i3-10100T CPU @ 3.00GHz``: the field rendered empty
    # while approval would have catalogued the reported string. Sticky, too - once blank, every
    # later load was blank, and there was no way to get the detected value back except by
    # knowing it and retyping it.
    #
    # The flip side, stated because it is a real consequence: clearing one of these fields
    # does not stick. It never did - approval falls back to the reported value - so the form
    # now shows what will actually be catalogued instead of implying the deletion took.
    #
    # ``""`` and ``None`` only. ``False`` and ``0`` are answers here: they carry the release
    # ticks and the minor floors, and skipping those would retract claims.
    for key, value in (run.listing_proposal or {}).items():
        blank = value is None or (isinstance(value, str) and not value.strip())
        if blank and str(initial.get(key) or "").strip():
            continue
        initial[key] = value
    # Release ticks are unioned in last, from every durable source: the catalog listing, the
    # submitter's other runs whatever their status, and **the release this run passed on**.
    # Borrowing only from *draft* siblings meant a run on 10 uploaded after 8, 9, and 10 had
    # been declared came back with 8 and 9 unticked, and saving that made the loss real.
    #
    # The run's own release belongs in this union, not only in the prefill underneath it.
    # ``detected_releases`` put it there, and a stored ``release_9: False`` sitting on top
    # then hid it: reported as the AlmaLinux boxes no longer being checked by default, on a
    # run that had passed on 9.8 with the 9 box rendering unticked. Nothing else would have
    # re-ticked it, since ``claimed_release_ticks`` reads the listing and the siblings and
    # deliberately not the run in hand.
    #
    # Suppressing a tick this way also contradicted the rule the merge exists to enforce: a
    # release claim can be added and never retracted, so an unticked box is not a retraction
    # and must not read as one.
    #
    # ``merge_listing_proposal`` and not ``update`` for this step, because a tick has to
    # survive being absent from the other side - and this way round, so ``initial`` stays
    # the base and keeps its text prefills. It also keeps the floors honest: a side's minor
    # counts only where that side claims the major, so a stored ``release_minor_9: 0`` left
    # behind by an unticked box cannot widen 9.8 into "all of 9.x".
    durable = services.merge_listing_proposal(
        services.claimed_release_ticks(run),
        RunListingProposalForm.detected_releases(run),
    )
    initial = services.merge_listing_proposal(durable, initial)
    # Attribution is not seeded here. The form's ``_build_attribution_field`` reads it off
    # the run, including the vendor-matches-the-hardware preselection, so a second source
    # would only be a way for the two to disagree.
    return initial


def _vendor_by_slug(slug: str):
    from lumina.vendors.models import Vendor

    if not slug:
        return None
    return Vendor.objects.filter(slug=slug).first()


@login_required
@require_POST
def submit_run_for_review(request: HttpRequest, uuid) -> HttpResponse:
    """Submitter releases their completed validation run into the queue."""
    from lumina.results import services

    run = get_object_or_404(TestRun, uuid=uuid, submitter=request.user)
    try:
        services.submit_for_review(run, by=request.user)
    except services.ReviewError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request, "Submitted for review. A reviewer will look at it shortly."
        )
    return HttpResponseRedirect(run.get_absolute_url())


@login_required
@require_POST
def submit_run_group_for_review(request: HttpRequest, uuid) -> HttpResponse:
    """Submit every unsubmitted run of this machine in one action."""
    run = get_object_or_404(TestRun, uuid=uuid, submitter=request.user)
    submitted, blocked = services.submit_group_for_review(run, by=request.user)

    if submitted:
        messages.success(
            request,
            f"Submitted {len(submitted)} run(s) for review: "
            f"{', '.join(str(r.alma_release or 'unknown release') for r in submitted)}.",
        )
    for member, reason in blocked:
        # Named individually: "one failed" is not actionable without knowing
        # which run and why.
        messages.error(
            request,
            f"{member.alma_release or member.uuid} was not submitted: {reason}",
        )
    return HttpResponseRedirect(run.get_absolute_url())


@login_required
@require_POST
def archive_run(request: HttpRequest, uuid) -> HttpResponse:
    """Put one of the submitter's own runs out of sight on their dashboard.

    Redirects back to wherever they pressed it, because this is used from two places and landing
    somewhere else after a filing action is disorienting.
    """
    run = get_object_or_404(TestRun, uuid=uuid, submitter=request.user)
    try:
        services.archive_run(run, by=request.user)
    except services.ReviewError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Archived. You can find it under Archived on your dashboard.")
    return HttpResponseRedirect(_back_to(request))


@login_required
@require_POST
def unarchive_run(request: HttpRequest, uuid) -> HttpResponse:
    run = get_object_or_404(TestRun, uuid=uuid, submitter=request.user)
    try:
        services.unarchive_run(run, by=request.user)
    except services.ReviewError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Restored.")
    return HttpResponseRedirect(_back_to(request))


def _back_to(request: HttpRequest) -> str:
    """The page the action was pressed on, or the dashboard.

    Only same-site paths are honored. ``next`` comes from a form field, and a redirect target
    taken from a request is an open redirect unless it is checked.
    """
    from django.urls import reverse
    from django.utils.http import url_has_allowed_host_and_scheme

    target = request.POST.get("next") or ""
    if target and url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure(),
    ):
        return target
    return reverse("accounts:dashboard")


def latest_validations(request: HttpRequest) -> HttpResponse:
    runs = (
        TestRun.objects.public()
        .filter(run_type=RunType.validate.value)
        .select_related("alma_release", "listing_system", "listing_system__vendor")
        .order_by("-published_at")[:50]
    )
    return render(request, "results/latest_validations.html", {"runs": runs})


_HEADLINE_METRICS = 3


def benchmark_index(request: HttpRequest) -> HttpResponse:
    catalog = result_filters.benchmark_catalog()
    for entry in catalog:
        # The catalog rows are dicts from an aggregate, so the label cannot come
        # from a model property here.
        entry["label"] = benchmark_label(entry["benchmark_id"])
        # And the section heading, for the same reason. ``capfirst`` on the slug read "Gpu".
        entry["category_label"] = category_label(entry["category"])
    # Re-sorted on the label, because ``regroup`` groups only *adjacent* rows: two slugs sharing a
    # label ("mem" and "memory") would otherwise open two identical sections, and the query's own
    # ordering is by slug. Ordering by the heading a reader sees is also the order they expect.
    catalog.sort(key=lambda entry: (entry["category_label"], entry["label"]))
    latest = list(
        TestRun.objects.public()
        .with_benchmarks()
        .order_by("-published_at")
        .prefetch_related("benchmarks")[:8]
    )
    # A combined run carries ~18 primary metrics; dumping them all made the
    # sidebar unreadable. Slicing the first few was no better: Meta ordering is
    # alphabetical by benchmark_id, so the three shown were the Python build
    # time and two compression numbers, with no CPU, memory, or disk figure in
    # sight. attach_headlines picks one per category, best first.
    latest = attach_headlines(latest, limit=_HEADLINE_METRICS)
    return render(
        request,
        "results/benchmarks_index.html",
        {"catalog": catalog, "latest_runs": latest},
    )


def benchmark_compare(request: HttpRequest) -> HttpResponse:
    """Compare hardware models side by side, averaged over every run of them.

    Subjects are CPU (and GPU) models rather than individual runs: "how does this
    CPU compare to that one" is the question, and one run answers it with a
    sample size of one. Selection lives in the query string so a comparison is a
    shareable link.
    """
    kind = request.GET.get("kind") or compare.DEFAULT_KIND
    if kind not in compare.SUBJECT_KINDS:
        kind = compare.DEFAULT_KIND
    keys = compare.parse_selection(request.GET.getlist("subject"))
    # visible_to, not public(): a submitter comparing against their own embargoed
    # result is legitimate, and reviewers see everything. Anonymous readers get
    # public runs only, which is what public() would have given them.
    runs = TestRun.objects.visible_to(request.user)
    context = {
        "kind": kind,
        "kinds": compare.SUBJECT_KINDS,
        "options": compare.subject_options(kind, runs=runs),
        "selected": keys,
        "max_compare": compare.MAX_COMPARE,
        "min_compare": compare.MIN_COMPARE,
        **compare.compare_subjects(kind, keys, runs=runs),
    }
    template = (
        "results/_compare_table.html" if request.htmx
        else "results/compare.html"
    )
    return render(request, template, context)


def leaderboard(request: HttpRequest, benchmark_id: str) -> HttpResponse:
    """Ranked results for one benchmark.

    Default view groups by the hardware that determines the score (GPU for
    graphics benchmarks, CPU otherwise), because "how fast is this CPU" is
    the question a reader arrives with. Selecting one piece of hardware, or
    asking for ``group=none``, drops to individual runs.
    """
    params = dict(request.GET.lists())
    version = (
        params.get("version", [None])[0]
        or result_filters.latest_version_for(benchmark_id)
    )
    if version is None:
        raise Http404("No public results for this benchmark.")
    metric = (
        params.get("metric", [None])[0]
        or result_filters.default_metric_for(benchmark_id, version)
    )
    params.setdefault("version", [version])

    # The natural dimension is the model - cpu or gpu. Family is a filter now,
    # not a grouping: a family's median is whatever mix of its models people
    # happened to submit, so ranking families against each other measures
    # submission habits rather than hardware. Narrowing to one family and
    # comparing the models inside it is the useful version of that question.
    natural_group = result_filters.group_field_for(benchmark_id)
    requested_group = params.get("group", [None])[0]
    picked_model = bool(params.get(natural_group, [""])[0])
    if requested_group:
        group_by = requested_group
    elif picked_model:
        # One model selected: grouping it against itself says nothing, so drop
        # to the individual runs behind it.
        group_by = "none"
    else:
        group_by = natural_group

    facets = result_filters.leaderboard_facets(benchmark_id, version)
    families = result_filters.leaderboard_families(
        benchmark_id, version, natural_group
    )
    grouping = None
    rows = []
    if group_by == "none":
        rows = list(
            result_filters.filter_leaderboard(
                benchmark_id=benchmark_id, params=params
            )[:100]
        )
        ceiling = max((float(r.value) for r in rows), default=0) or 1
        for rank, row in enumerate(rows, start=1):
            row.rank = rank
            row.percent = round(float(row.value) / ceiling * 100, 1)
    else:
        grouping = result_filters.leaderboard_groups(
            benchmark_id=benchmark_id, params=params, group_by=group_by
        )

    context = {
        "benchmark_id": benchmark_id,
        "benchmark_label": benchmark_label(benchmark_id),
        "version": version,
        "metric": metric,
        "rows": rows,
        "grouping": grouping,
        "group_by": group_by,
        "natural_group": natural_group,
        "group_options": result_filters.GROUP_FIELDS,
        "facets": facets,
        "families": families,
        "family_param": f"{natural_group}_family",
        "dimension": natural_group,
        "selected": {key: params.get(key, [""])[0] for key in
                     ("cpu", "cpu_family", "gpu", "gpu_family", "gpu_driver",
                      "vendor", "alma", "metric", "group",
                      "sockets", "memory_type", "memory_speed")},
        # The metric picker, broken down by API and by clpeak's own categories. Empty for every
        # benchmark whose metrics carry no API, and the template falls back to the flat list.
        "metric_sections": gpu_metrics.grouped(facets.get("metrics") or []),
        # And what the current selection is, so the heading can say which API produced the number
        # rather than printing the key and leaving the reader to parse it.
        "metric_described": gpu_metrics.describe(metric or ""),
    }
    if request.htmx:
        return render(request, "results/_leaderboard_rows.html", context)
    return render(request, "results/leaderboard.html", context)


def stats(request: HttpRequest) -> HttpResponse:
    data = cache.get_or_set(
        "results:hardware-stats", result_filters.hardware_stats, 600
    )
    return render(request, "results/stats.html", {"stats": data})
