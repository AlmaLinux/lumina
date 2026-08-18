"""Template helpers for the reviewer interface."""
from __future__ import annotations

from django import template

from lumina.review import queue_counts

register = template.Library()


@register.simple_tag
def review_queue_total() -> int:
    """How much is waiting on a reviewer, across every queue.

    A tag rather than a context processor: this runs a handful of COUNT queries, and a
    context processor would run them for every page render including the ones that never
    show the sidebar. Only the sidebar asks, and only for a reviewer.
    """
    return queue_counts.total()
