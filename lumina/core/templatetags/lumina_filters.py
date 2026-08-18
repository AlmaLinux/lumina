"""Small template helpers used by the catalog filter panel.

``approved_values_only`` filters a CategoryValue queryset/list down to
approved entries. Doing this in the template keeps ``views.py`` from having
to pre-annotate each Category with a second queryset.

``get_item`` looks up a dict key from the template since Django templates
don't support ``dict[key]`` syntax natively. Used to show checked state for
values that appear in the active_filters query dict.

``level_short`` renders a trust tier without its "-validated" suffix, for badges
under a column that already names the concept. See
``lumina.core.certification.short_label``.
"""
from __future__ import annotations

from collections.abc import Iterable

from django import template

from lumina.core.certification import ValidationLevel, short_label
from lumina.taxonomy.models import CategoryValue

register = template.Library()


@register.filter
def level_short(level: str) -> str:
    return short_label(level)


@register.filter
def unslug(value: str) -> str:
    """``confidential_computing`` -> ``confidential computing``.

    For displaying data keys as labels. Django has no ``replace`` filter, and the
    keys have to stay snake_case where they are data - the report, the API - so the
    conversion belongs at the point of display.
    """
    return str(value).replace("_", " ").replace("-", " ")


@register.filter
def approved_values_only(values: Iterable[CategoryValue]) -> list[CategoryValue]:
    return [v for v in values if v.status == CategoryValue.STATUS_APPROVED]


@register.filter
def get_item(mapping: dict | None, key: str) -> list[str]:
    if not mapping:
        return []
    return mapping.get(key, []) or []


@register.filter
def class_name(obj: object) -> str:
    """Return the runtime class name - used by templates to branch between
    System and Component rendering without bloating the view context."""
    return type(obj).__name__


@register.simple_tag
def validation_levels() -> list:
    """``ValidationLevel.choices``, for templates that render a tier picker.

    A tag rather than view context, so neither of the two review templates that
    render the reviewer's final-tier dropdown has to be given it - and so a third
    caller cannot forget to. Both used to spell out all three value/label pairs, which
    meant a fourth tier would be accepted by the views (they validate against the
    enum) and offerable by neither page.
    """
    return list(ValidationLevel.choices)


@register.filter
def is_reviewer(user) -> bool:
    """Whether ``user`` may actually use the review UI.

    The sidebar gated its Review section on ``request.user.groups.exists``, which is not the same
    question: ``reviewer_required`` admits the ``reviewer`` and ``admin`` groups only. Any other
    group is enough to be shown the links and not enough to follow them, so a member of
    ``certifier`` - a group the devstack seed creates - saw a Review section that led to a
    plain-text 403. One predicate now, imported from the module that enforces it, so the menu and
    the door cannot drift apart again.
    """
    from lumina.review.permissions import is_reviewer as _is_reviewer

    return _is_reviewer(user)
