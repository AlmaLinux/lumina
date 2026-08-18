"""Software catalog filtering.

The single source of truth for both the HTML browse page and the JSON API, the
same arrangement ``hardware.filters.filter_listings`` has, so the two surfaces
cannot drift apart.

Category filtering is delegated to ``taxonomy.filters.apply_category_filters``,
which the hardware catalog also uses - the rule is identical and only the join
field differs.

Filter semantics:
  - Unpublished software is never returned.
  - ``q``: name, vendor name, or description.
  - ``vendor=<slug>`` (repeatable): OR within.
  - ``alma=<major>`` (repeatable): OR within, and **approved majors only**.
  - ``<category-slug>=<value-slug>``: OR within a category, AND across.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from django.db.models import Q, QuerySet

from lumina.software.models import Software, SoftwareCompatibility
from lumina.taxonomy.filters import apply_category_filters

# Parameter names that mean something other than a category slug.
# ``vendor_q`` searches the vendor *facet*, not the catalog. It rides along in the
# query string so a reloaded page keeps the vendor list you had narrowed, which
# means it has to be reserved here or a category named "vendor_q" would collide.
_RESERVED_PARAMS = frozenset({"vendor", "q", "page", "alma", "vendor_q"})


def filter_software(*, params: Mapping[str, Sequence[str]]) -> QuerySet[Software]:
    qs = Software.objects.filter(published=True)

    q_values = params.get("q") or []
    # First non-blank only; an empty search box must not empty the catalog.
    q = next((v.strip() for v in q_values if v and v.strip()), "")
    if q:
        qs = qs.filter(
            Q(name__icontains=q)
            | Q(vendor__name__icontains=q)
            | Q(description__icontains=q)
        )

    vendors = params.get("vendor") or []
    if vendors:
        qs = qs.filter(vendor__slug__in=vendors)

    alma_majors: list[int] = []
    for value in params.get("alma") or []:
        try:
            alma_majors.append(int(value))
        except ValueError:
            # Same silent-ignore stance as a typo'd category param.
            continue
    if alma_majors:
        # Both conditions in one ``filter`` so they apply to the same joined row.
        # Split across two calls, a product with an approved 9 and a pending 10
        # would match ``alma=10``, advertising a release no reviewer has accepted.
        qs = qs.filter(
            compatibility__status=SoftwareCompatibility.STATUS_APPROVED,
            compatibility__release__major__in=alma_majors,
        )

    qs = apply_category_filters(
        qs,
        model=Software,
        params=params,
        join_field="software",
        reserved=_RESERVED_PARAMS,
    )

    return qs.distinct()
