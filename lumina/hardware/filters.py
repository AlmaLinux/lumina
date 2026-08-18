"""Catalog filtering.

``filter_listings`` is the single source of truth used by both the HTML
browse pages and the JSON API. Given a listing model (System or Component)
and a dict-of-lists of query-string parameters, it returns a filtered
queryset of published listings.

Filter semantics:
  - Unpublished listings are never returned.
  - ``vendor=<slug>`` (repeatable): OR within.
  - ``<category-slug>=<value-slug>`` (repeatable): OR within a category,
    AND across categories.
  - Values in ``pending``/``rejected`` status never match.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from django.db.models import Q, QuerySet

from lumina.hardware.models import (
    Component,
    ComponentKind,
    HardwareListing,
    listing_fk_name,
)
from lumina.taxonomy.filters import apply_category_filters

# Parameter names reserved for non-category filters; category filters use
# whatever Category slugs exist in the database.
# ``vendor_q`` searches the vendor *facet*, not the catalog, and rides along in the
# query string so a reloaded page keeps a narrowed vendor list. Reserved so a
# category could never be named it.
_RESERVED_PARAMS = frozenset({"vendor", "q", "page", "alma", "kind", "vendor_q"})


def filter_listings[ListingT: HardwareListing](
    model: type[ListingT],
    *,
    params: Mapping[str, Sequence[str]],
) -> QuerySet[ListingT]:
    qs: QuerySet[ListingT] = model.objects.filter(published=True)

    q_values = params.get("q") or []
    # Only the first q= takes effect; repeated q= across multiple segments
    # isn't a UX we expose. Empty strings are ignored so a blank search box
    # doesn't nuke the result set.
    q = next((v.strip() for v in q_values if v and v.strip()), "")
    if q:
        qs = qs.filter(
            Q(name__icontains=q)
            | Q(model_number__icontains=q)
            | Q(vendor__name__icontains=q)
        )

    vendors = params.get("vendor") or []
    if vendors:
        qs = qs.filter(vendor__slug__in=vendors)

    # ?kind=motherboard&kind=gpu - Component browsing only (Systems have no
    # kind); OR semantics like every other multi-select filter here. Unknown
    # values are dropped, and if nothing valid remains the filter is not
    # applied at all - same silent-ignore stance as typo'd category params.
    if model is Component:
        valid = {k.value for k in ComponentKind}
        kinds = [v for v in (params.get("kind") or []) if v in valid]
        if kinds:
            qs = qs.filter(kind__in=kinds)

    # ?alma=9&alma=10 filters to listings that have a ListingVersion row
    # for at least one of the selected AlmaLinux majors (OR semantics).
    # A major is the whole claim now. This filter never looked at the per-major minor floor
    # that used to exist - "show me things that run on AlmaLinux 9" is the question people
    # ask - and that being the only consumer's intent is part of why the floor went.
    alma_majors: list[int] = []
    for v in params.get("alma") or []:
        try:
            alma_majors.append(int(v))
        except ValueError:
            continue
    if alma_majors:
        qs = qs.filter(versions__release__major__in=alma_majors)

    qs = apply_category_filters(
        qs,
        model=model,
        params=params,
        join_field=listing_fk_name(model),
        reserved=_RESERVED_PARAMS,
    )

    return qs.distinct()
