"""Public catalog browse and detail views.

Browse views share a single implementation - the only difference between
Systems and Components is which model we filter and which applies_to scope
we show in the filter panel.

HTMX integration: when the request carries ``HX-Request: true`` we render
just the results partial, letting the filter panel swap it in without a
full page reload. Plain GETs still render the complete page.
"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.http import (
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseForbidden,
    HttpResponseRedirect,
)
from django.shortcuts import render
from django.urls import reverse

from lumina.core.http import params, redirect_preserving_query, vendor_facet_context
from lumina.hardware.filters import filter_listings
from lumina.hardware.forms import ListingEditProposalForm
from lumina.hardware.models import (
    Component,
    ComponentKind,
    HardwareListing,
    ListingVersion,
    System,
)
from lumina.releases.models import AlmaLinuxRelease
from lumina.taxonomy.models import Category
from lumina.vendors.services import (
    can_edit_listing,
    is_claimable,
)


def _filter_panel_categories(applies_to_scope: str) -> list[Category]:
    # The catalog's "Hardware" category from catalog.redhat.com inspiration
    # is represented as applies_to=both; include those plus the kind-specific
    # categories for this page.
    return list(
        Category.objects.filter(
            applies_to__in=[applies_to_scope, Category.APPLIES_BOTH]
        ).prefetch_related("values")
    )


def _vendor_facet_context(
    request: HttpRequest, model: type[HardwareListing], kind: str
) -> dict:
    """Thin wrapper over the shared helper: hardware's search URL is kind-dependent.

    Kept as a wrapper rather than inlined at the call sites because the ``kind`` ->
    URL mapping is the one hardware-specific part.
    """
    return vendor_facet_context(
        request, model, search_url=reverse("hardware:vendor_search", args=[kind]),
    )


def vendor_search(request: HttpRequest, kind: str) -> HttpResponse:
    """Re-render just the vendor checkboxes for a search term.

    Server-side, because the rendered list is a window: filtering it in the browser
    would answer "no such vendor" for anything outside that window.

    A non-HTMX request is bounced to the catalog rather than served the fragment:
    the search box lives inside the filter form, so a browser can reach this URL by
    submitting it, and a partial answered to a navigation becomes the whole page.
    """
    if kind not in ("systems", "components"):
        raise Http404("No such catalog.")
    if request.headers.get("HX-Request") != "true":
        return redirect_preserving_query(request, f"hardware:{kind}")
    model = System if kind == "systems" else Component
    return render(
        request,
        "catalog/_vendor_options.html",
        {
            **_vendor_facet_context(request, model, kind),
            "active_filters": params(request),
        },
    )


def _browse(request: HttpRequest, model: type[HardwareListing], kind: str) -> HttpResponse:
    qs = filter_listings(model, params=params(request)).select_related("vendor")
    categories = _filter_panel_categories(
        Category.APPLIES_SYSTEM if kind == "systems" else Category.APPLIES_COMPONENT
    )
    ctx = {
        "listings": qs,
        "categories": categories,
        **_vendor_facet_context(request, model, kind),
        "releases": AlmaLinuxRelease.objects.supported(),
        "kind": kind,
        "component_kinds": [
            {"value": k.value, "label": k.label} for k in ComponentKind
        ],
        "active_filters": params(request),
    }
    template = (
        "hardware/_results.html"
        if request.headers.get("HX-Request") == "true"
        else "hardware/browse.html"
    )
    return render(request, template, ctx)


def systems(request: HttpRequest) -> HttpResponse:
    return _browse(request, System, "systems")


def components(request: HttpRequest) -> HttpResponse:
    return _browse(request, Component, "components")


def detail(request: HttpRequest, slug: str) -> HttpResponse:
    # Systems and Components share a unique slug space (each model enforces
    # its own unique slug and the autoslug helper prefixes with vendor, so
    # cross-model collisions are effectively impossible).
    for model in (System, Component):
        obj = model.objects.filter(slug=slug, published=True).select_related("vendor", "owner_vendor").first()
        if obj is not None:
            return render(
                request,
                "hardware/detail.html",
                {
                    "listing": obj,
                    "user_can_edit": can_edit_listing(request.user, obj),
                    # Computed here because the rule runs a query and the
                    # template would run it inside the render.
                    "vendor_claimable": is_claimable(obj.vendor),
                    **_compatibility_context(obj),
                    **_certification_context(request, obj),
                    **_family_context(obj),
                    **_cpu_support_context(obj),
                    **_used_in_context(obj),
                    "has_declared_versions": obj.versions.filter(
                        source=ListingVersion.SOURCE_DECLARED
                    ).exists(),
                },
            )
    raise Http404("No published listing with this slug.")


def _compatibility_context(listing) -> dict:
    """The release table's rows, plus the community total the header shows.

    Evaluated to a list here rather than handed to the template as a queryset,
    because the header needs a total over the same rows and iterating a queryset
    twice would run the whole thing twice.
    """
    rows = list(_compatibility(listing))
    return {
        "compatibility": rows,
        "community_total": sum(row.community_confirmations() for row in rows),
        # Passed rather than hardcoded in the shared card, which serves both
        # catalogs and cannot know that hardware has no action column.
        "compatibility_columns": [
            "Release", "Certified by", "Community confirmations",
        ],
        "compatibility_note": (
            "A release certified by its vendor or by AlmaLinux still shows how many "
            "community members independently confirmed it by running the suite. The "
            "badge at the top of the page is the highest level across all releases, "
            "so check this table for the release you care about."
        ),
    }


def _compatibility(listing):
    """Cited AlmaLinux releases, newest first, with their evidence split in two.

    ``official_levels`` and ``community_confirmations`` both walk this release's
    attestations, so the rows are **prefetched**: without that the table would run
    two queries per release to answer questions one query already has the data for.

    ``attestation_total`` rather than ``attestation_count``: the listing carries a
    field by that name holding a different number, and shadowing it in the template
    context is how somebody ends up reading the wrong one.
    """
    return (
        listing.versions.select_related("release")
        .prefetch_related("attestations")
        .annotate(attestation_total=Count("attestations", distinct=True))
        .order_by("-release__major")
    )


def _cpu_support_context(listing) -> dict:
    """Validated and vendor-declared CPU families, in one ordered list."""
    if not isinstance(listing, System):
        return {}
    support = listing.cpu_support()
    return {
        "cpu_support": support,
        # Drives the explanatory note: with nothing declared there is no
        # distinction to explain, so the note would just be noise.
        "has_declared_cpus": any(not entry["validated"] for entry in support),
    }


# Enough to be useful on one screen without an unbounded page. A widely used
# CPU family will exceed it long before pagination is worth building.
SYSTEMS_SHOWN = 24


def _used_in_context(listing) -> dict:
    """The systems a component turns up in, so the catalog connects both ways."""
    if not isinstance(listing, Component):
        return {}
    used_in = listing.used_in_systems()
    return {
        "used_in": used_in[:SYSTEMS_SHOWN],
        "used_in_total": len(used_in),
        "used_in_hidden": max(0, len(used_in) - SYSTEMS_SHOWN),
    }


def _family_context(listing) -> dict:
    """Expose the family/model relationship so it is visible rather than
    something that silently happens during grouping."""
    if not isinstance(listing, Component):
        return {}
    if listing.is_family:
        return {"family_models": list(listing.matching_models())}
    return {"parent_family": listing.resolved_family()}


# A page of certification results. Ten is where the table stops being scannable
# and starts pushing the rest of the listing off screen.
RUNS_PER_PAGE = 10


def _certification_context(request: HttpRequest, listing) -> dict:
    """A filtered, paginated page of this listing's validation runs.

    Server-side, and not because a browser-side filter would be slow. The page
    costs several queries **per run** - ``status_counts`` and ``run_trust_level``
    each hit the database per row - so the cost is paid building the rows and
    hiding them client-side would save none of it. Paging bounds it. It also makes
    the deep link from the compatibility table an ordinary GET, so it is shareable
    and works with scripting off.

    This replaces a bare ``[:20]`` slice, which showed twenty runs and said nothing
    about any others.
    """
    from django.core.paginator import Paginator

    from lumina.results.models import RunType, TestRun

    link = (
        {"listing_system": listing}
        if isinstance(listing, System)
        else {"listing_components": listing}
    )
    runs = (
        TestRun.objects.public()
        .filter(run_type=RunType.validate.value, **link)
        .select_related("alma_release", "submitter")
        .order_by("-published_at")
    )

    # Offered only for releases that actually have runs: a filter returning
    # nothing reads as "no such evidence" when it means "nobody ran that release".
    #
    # ``order_by()`` clears the inherited ``-published_at`` before the DISTINCT.
    # Without it Django adds ``published_at`` to the SELECT so it can order by it,
    # DISTINCT then applies to the (major, timestamp) pair, and every run yields its
    # own "distinct" major - AlmaLinux 10 was listed once per run on it.
    available = sorted(
        (
            major for major in runs
            .exclude(alma_release__isnull=True)
            .order_by()
            .values_list("alma_release__major", flat=True)
            .distinct()
        ),
        reverse=True,
    )

    selected = request.GET.get("cert_alma", "")
    try:
        selected_major = int(selected)
    except ValueError:
        selected_major = None
    # Silently ignored rather than 404: a stale or hand-edited URL should show the
    # unfiltered table, the same stance the catalog filters take on a typo.
    if selected_major not in available:
        selected_major = None
    if selected_major is not None:
        runs = runs.filter(alma_release__major=selected_major)

    paginator = Paginator(runs, RUNS_PER_PAGE)
    page = paginator.get_page(request.GET.get("cert_page"))
    _annotate_runs(page.object_list, listing)

    return {
        "cert_page": page,
        "cert_total": paginator.count,
        "cert_alma_options": available,
        "cert_alma_selected": selected_major,
    }


def _annotate_runs(runs, listing) -> None:
    """Attach the two per-row facts, in a fixed number of queries.

    Both used to be computed per run - ``status_counts`` is a grouped query on the
    run's results, and ``run_trust_level`` reads the run's attestation and falls
    back to deriving the submitter's entitlement, which is two more. On a page of
    ten that is forty-odd queries for a public page.
    """
    from django.db.models import Count

    from lumina.results.models import TestResult
    from lumina.results.services import run_trust_level

    runs = list(runs)
    if not runs:
        return

    # One grouped query for every row's test-status tally.
    counts: dict[int, dict[str, int]] = {run.pk: {} for run in runs}
    tallies = (
        TestResult.objects.filter(run__in=runs)
        .values("run_id", "status")
        .annotate(n=Count("id"))
    )
    for row in tallies:
        counts[row["run_id"]][row["status"]] = row["n"]

    # Who stands behind each piece of evidence matters as much as the result: a
    # vendor's own validation reads differently from a community member's.
    #
    # One query for the frozen levels rather than one per run. ``run_trust_level``
    # prefers the level recorded on the run's own attestation, so this cannot be
    # cached per submitter - the same person's two runs can carry different levels,
    # and one of them may have no attestation at all.
    from lumina.hardware.models import CommunityAttestation

    fk = (
        "listing_system" if isinstance(listing, System) else "listing_component"
    )
    frozen = dict(
        CommunityAttestation.objects
        .filter(test_run__in=runs, **{fk: listing})
        .values_list("test_run_id", "level")
    )

    # Only the runs with no attestation need deriving, and that answer depends on
    # exactly these three things - so two runs agreeing on all of them agree on the
    # tier, and the expensive group/membership lookups happen once per distinct
    # combination instead of once per run.
    derived: dict[tuple, str] = {}
    for run in runs:
        run.tallied_statuses = counts[run.pk]
        if run.pk in frozen:
            run.trust_level = frozen[run.pk]
            continue
        key = (run.submitter_id, run.on_behalf_of_id, run.claimed_validation_level)
        if key not in derived:
            derived[key] = run_trust_level(run, listing)
        run.trust_level = derived[key]


def _get_listing_for_edit(slug: str) -> HardwareListing:
    for model in (System, Component):
        obj = model.objects.filter(slug=slug).select_related("owner_vendor").first()
        if obj is not None:
            return obj
    raise Http404("No listing with this slug.")


@login_required
def propose_edit(request: HttpRequest, slug: str) -> HttpResponse:
    listing = _get_listing_for_edit(slug)
    if not can_edit_listing(request.user, listing):
        return HttpResponseForbidden(
            "You need a submit-role membership in this listing's owner vendor "
            "to propose edits."
        )
    if request.method == "POST":
        form = ListingEditProposalForm(request.POST, listing=listing)
        if form.is_valid():
            proposal = form.save(commit=False)
            proposal.proposed_by = request.user
            if isinstance(listing, System):
                proposal.listing_system = listing
            else:
                proposal.listing_component = listing
            proposal.save()
            messages.success(request, f"Edit proposal for {listing} submitted for review.")
            return HttpResponseRedirect(reverse("hardware:detail", args=[listing.slug]))
    else:
        form = ListingEditProposalForm(listing=listing)
    return render(
        request,
        "hardware/propose_edit.html",
        {"form": form, "listing": listing},
    )
