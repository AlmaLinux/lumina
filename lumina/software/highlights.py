"""Home-page feeds for the software catalog.

Kept in the software app rather than written inline in ``core.views`` so the home
page's context does not become the one place every app's queries are spelled out.
Mirrors ``results.highlights``, which does the same job for benchmark runs.

Both feeds are keyed on a **timestamp**, not on a total, so they turn over as
people use the site instead of settling on whatever is most popular and then
looking abandoned. That is only possible because each attestation and each
certification is its own row with its own ``auto_now_add`` timestamp.
"""
from __future__ import annotations

from django.db.models import Count, Max, QuerySet

from lumina.software.models import Software, SoftwareCompatibility

FEED_LENGTH = 6

# Only approved majors. A pending row is one person's unreviewed claim about
# somebody else's product, and the home page is the last place it should surface.
# Filtering *before* annotating is what keeps it out of the aggregates too: the
# filter constrains the same join the aggregate walks, so an unapproved release
# cannot set the timestamp that decides the ordering.
_APPROVED = {"compatibility__status": SoftwareCompatibility.STATUS_APPROVED}


def recently_validated(limit: int = FEED_LENGTH) -> QuerySet[Software]:
    """Products by how recently they were officially certified, newest first.

    One entry per *product*, not per certification. A vendor certifying one
    product on 8, 9, and 10 in a single sitting would otherwise fill the whole
    feed with that product and read as three unrelated events.
    """
    return (
        Software.objects.published()
        .filter(**_APPROVED)
        .annotate(validated_at=Max("compatibility__certifications__certified_at"))
        .filter(validated_at__isnull=False)
        .select_related("vendor")
        .order_by("-validated_at")[:limit]
    )


def recently_confirmed(limit: int = FEED_LENGTH) -> QuerySet[Software]:
    """Products the community most recently confirmed, newest first.

    Ordered by the newest confirmation rather than by total count: a popular
    product should not hold the top slot forever while newer activity goes
    unseen.

    Both aggregates walk the same ``compatibility__attestations`` join, so they
    do not multiply each other the way aggregates over two *different*
    multi-valued relations would - the classic Django join-fanout bug, where
    adding a second Count silently inflates the first.
    """
    return (
        Software.objects.published()
        .filter(**_APPROVED)
        .annotate(
            confirmed_at=Max("compatibility__attestations__created_at"),
            confirmations=Count("compatibility__attestations", distinct=True),
        )
        .filter(confirmed_at__isnull=False)
        .select_related("vendor")
        .order_by("-confirmed_at")[:limit]
    )
