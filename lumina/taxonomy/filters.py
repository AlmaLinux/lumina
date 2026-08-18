"""Turning query-string parameters into taxonomy filters.

Lives in the taxonomy app rather than in a catalog app because both catalogs
filter by category and the rule is identical in each: any parameter whose name
matches a ``Category.slug`` narrows the queryset to listings bound to one of the
named values.

What differs between the catalogs is only how a listing reaches its bindings -
hardware's join table has a nullable FK per listing kind, software's has one -
so ``join_field`` carries that and nothing else does.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from django.db.models import QuerySet

from lumina.taxonomy.models import Category, CategoryValue


def first_query_term(params: Mapping[str, Sequence[str]]) -> str:
    """The first non-blank ``?q=`` search term, or ``""``.

    Empty strings are ignored so a blank search box does not empty the catalog, and only the first
    ``q=`` takes effect. Shared by both catalog filters (hardware listings, software); each applies
    the returned term to its own set of searchable fields.
    """
    return next((v.strip() for v in (params.get("q") or []) if v and v.strip()), "")


def apply_category_filters[ModelT](
    qs: QuerySet[ModelT],
    *,
    model: type[ModelT],
    params: Mapping[str, Sequence[str]],
    join_field: str,
    reserved: Iterable[str],
    binding_related_name: str = "category_values",
) -> QuerySet[ModelT]:
    """Narrow ``qs`` by every category parameter present in ``params``.

    OR within a category (via ``__in``), AND across categories (via one
    subquery per category). Only ``approved`` values can match, so a value still
    waiting in the review queue never silently changes a public result set.

    Unrecognised parameter names are ignored rather than raising: the set of
    valid names is whatever ``Category`` rows exist, so a typo in a URL should
    return unfiltered results instead of a 500.

    ``join_field`` names the FK on the binding model that points back at the
    listing, and is also used to require that FK non-null - which is what keeps a
    hardware System from matching through a Component's bindings on the shared
    join table.
    """
    candidate_slugs = [name for name in params if name not in set(reserved)]
    if not candidate_slugs:
        return qs

    # Resolved against the database so an unknown parameter name is simply not a
    # category, rather than an error.
    category_slugs = set(
        Category.objects.filter(slug__in=candidate_slugs).values_list("slug", flat=True)
    )

    for slug in category_slugs:
        values = params.get(slug) or []
        if not values:
            continue
        matched_ids = (
            CategoryValue.objects.approved()
            .filter(category__slug=slug, slug__in=values)
            .values_list("pk", flat=True)
        )
        qs = qs.filter(
            pk__in=model.objects.filter(
                **{
                    f"{binding_related_name}__value__in": matched_ids,
                    f"{binding_related_name}__{join_field}__isnull": False,
                }
            ).values("pk")
        )

    return qs
