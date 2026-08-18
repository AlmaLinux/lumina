"""Filtering and paginating a listing's certification results.

The compatibility table says "3 confirmations" for AlmaLinux 9; the evidence behind
that number is further down the page in the certification results table. Clicking
the count now takes you there with the release already filtered, so the two halves
of the page are connected rather than left for the reader to correlate by eye.

Both the filter and the paging are **server-side**, and not because JavaScript
would be slow. Two reasons that actually decide it:

- The page costs roughly four queries *per run* - ``status_counts`` and
  ``run_trust_level`` each hit the database per row. That cost is paid building the
  rows, so hiding them in the browser saves nothing. Paging at ten bounds it.
- A rendered filter makes the deep link an ordinary GET: shareable, bookmarkable,
  and working with scripting off. A browser-side filter would need JavaScript to
  re-apply the query parameter after load.

It also replaces a silent ``[:20]`` slice. A listing with 25 runs showed 20 of them
and said nothing about the rest.
"""
from __future__ import annotations

import re

import pytest
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.urls import reverse
from django.utils import timezone

from lumina.core.certification import ValidationLevel
from lumina.hardware.models import (
    CommunityAttestation,
    ListingVersion,
    Submission,
    System,
)
from lumina.hardware.services import recompute_listing_levels
from lumina.releases.models import AlmaLinuxRelease
from lumina.results.models import RunType, TestRun
from lumina.vendors.models import Vendor

pytestmark = pytest.mark.django_db
User = get_user_model()

RUNS_PER_PAGE = 10


@pytest.fixture(autouse=True)
def releases():
    for major in (8, 9, 10):
        AlmaLinuxRelease.objects.get_or_create(
            major=major, defaults={"supported": True},
        )


@pytest.fixture
def system():
    vendor = Vendor.objects.create(name="Dell Inc.", published=True)
    return System.objects.create(
        vendor=vendor, name="PowerEdge R750", published=True,
    )


def _run(system, major, *, submitter=None, minor=0):
    """A published, approved validation run against this listing.

    Built directly rather than through ``ingest``, which needs a whole bundle - a
    pagination test needs dozens of these and what is *inside* them is irrelevant.
    """
    submitter = submitter or User.objects.create_user(f"u{TestRun.objects.count()}")
    return TestRun.objects.create(
        run_type=RunType.validate.value,
        schema_version="1.0", suite_version="0.1.0",
        submitter=submitter, source="api",
        bundle=ContentFile(b"x", name=f"b{TestRun.objects.count()}.tar.zst"),
        bundle_sha256=f"{TestRun.objects.count():064d}",
        status=TestRun.STATUS_APPROVED,
        published_at=timezone.now(),
        alma_release=AlmaLinuxRelease.objects.get(major=major),
        alma_minor=minor,
        listing_system=system,
    )


def _confirm(system, major, who):
    """A community confirmation on a release, so the count is clickable."""
    version, _ = ListingVersion.objects.get_or_create(
        listing_system=system,
        release=AlmaLinuxRelease.objects.get(major=major),
        defaults={"source": ListingVersion.SOURCE_RUN},
    )
    user = User.objects.create_user(who)
    CommunityAttestation.objects.create(
        version=version, listing_system=system,
        level=ValidationLevel.COMMUNITY, attested_by=user,
        submission=Submission.objects.create(
            submitter=user, listing_system=system,
            claimed_validation_level=ValidationLevel.COMMUNITY,
        ),
    )
    recompute_listing_levels(system)
    return version


def _page(client, system, **params):
    return client.get(
        reverse("hardware:detail", args=[system.slug]), params
    ).content.decode()


def _results_card(body: str) -> str:
    start = body.index('id="certification-results"')
    return body[start:body.index("</table>", start)]


def _majors_listed(card: str) -> list[str]:
    rows = re.findall(r"<tr>\s*<td class=\"text-secondary small\">.*?</tr>", card, re.S)
    return [
        re.search(r"<td>([\d.]*)</td>", row).group(1)
        for row in rows if re.search(r"<td>([\d.]*)</td>", row)
    ]


# --- the deep link ------------------------------------------------------------


def test_the_confirmation_count_links_to_the_filtered_results(client, system):
    """The two halves of the page were left for the reader to correlate by eye."""
    _confirm(system, 9, "fan")
    _run(system, 9)

    body = _page(client, system)

    assert 'href="?cert_alma=9#certification-results"' in body


def test_the_link_carries_the_release_it_was_clicked_on(client, system):
    _confirm(system, 9, "fan9")
    _confirm(system, 10, "fan10")
    _run(system, 9)
    _run(system, 10)

    body = _page(client, system)

    assert 'href="?cert_alma=9#certification-results"' in body
    assert 'href="?cert_alma=10#certification-results"' in body


def test_a_release_with_no_confirmations_offers_no_link(client, system):
    """Nothing to count, so nothing to click. The filter control on the results
    card is still there for anyone who wants that release's runs."""
    ListingVersion.objects.create(
        listing_system=system,
        release=AlmaLinuxRelease.objects.get(major=8),
        source=ListingVersion.SOURCE_DECLARED,
    )

    body = _page(client, system)

    assert "none yet" in body
    assert 'cert_alma=8#certification-results' not in body


# --- filtering ----------------------------------------------------------------


def test_filtering_narrows_the_results_to_one_release(client, system):
    for major in (8, 9, 10):
        _run(system, major)

    card = _results_card(_page(client, system, cert_alma=9))

    assert _majors_listed(card) == ["9.0"]


def test_no_filter_shows_every_release(client, system):
    for major in (8, 9, 10):
        _run(system, major)

    card = _results_card(_page(client, system))

    assert sorted(_majors_listed(card)) == ["10.0", "8.0", "9.0"]


def test_the_filter_offers_only_releases_that_have_runs(client, system):
    """A filter that returns nothing reads as "no such evidence" when it means
    "nobody ran that release"."""
    _run(system, 9)
    _run(system, 10)

    body = _page(client, system)

    assert "cert_alma=9" in body
    assert "cert_alma=10" in body
    assert "cert_alma=8" not in body


def test_each_release_is_offered_once(client, system):
    """Two runs on one release offered that release twice.

    ``.values_list("alma_release__major").distinct()`` inherited the queryset's
    ``order_by("-published_at")``, so Django added ``published_at`` to the SELECT to
    order by it and DISTINCT applied to the *pair*. Every run produced its own
    "distinct" major.
    """
    for _ in range(3):
        _run(system, 10)
    _run(system, 9)

    card = _results_card(_page(client, system))
    offered = re.findall(r'href="\?cert_alma=(\d+)#certification-results"', card)

    assert offered == ["10", "9"], offered


def test_the_filter_says_it_is_filtering_almalinux_releases(client, system):
    """Bare numbers in a button group do not say what they are numbers of.

    Asserted on visible element text rather than a substring: the group already
    carried ``aria-label="Filter by AlmaLinux release"``, so a plain ``in card``
    passed while the screen still showed an unlabelled row of numbers.
    """
    _run(system, 9)
    _run(system, 10)

    card = _results_card(_page(client, system))

    assert re.search(r">\s*AlmaLinux release\s*<", card), (
        "the label is not rendered as visible text"
    )


def test_a_nonsense_filter_is_ignored_rather_than_crashing(client, system):
    _run(system, 9)

    for junk in ("banana", "", "999"):
        body = _page(client, system, cert_alma=junk)
        assert "certification-results" in body


def test_the_active_filter_is_marked_as_active(client, system):
    _run(system, 9)
    _run(system, 10)

    body = _page(client, system, cert_alma=9)

    card = _results_card(body)
    # The marker must be on the release that was clicked and not on the others. The
    # old "active or btn-primary" check was satisfied by the unselected "All" chip,
    # so inverting the template so every chip *except* the clicked one is marked
    # still passed.
    # The whole opening tag: the class attribute precedes href in the template.
    nine = re.search(r'<a[^>]*cert_alma=9#certification-results[^>]*>', card)
    ten = re.search(r'<a[^>]*cert_alma=10#certification-results[^>]*>', card)
    assert nine and ten
    assert "active" in nine.group(0) or "btn-primary" in nine.group(0)
    assert "active" not in ten.group(0)


# --- pagination ---------------------------------------------------------------


def test_a_short_list_is_not_paginated(client, system):
    for _ in range(RUNS_PER_PAGE):
        _run(system, 9)

    body = _page(client, system)

    assert len(_majors_listed(_results_card(body))) == RUNS_PER_PAGE
    assert "cert_page=2" not in body


def test_past_the_page_size_the_rest_is_on_later_pages(client, system):
    for _ in range(RUNS_PER_PAGE + 5):
        _run(system, 9)

    first = _page(client, system)
    second = _page(client, system, cert_page=2)

    assert len(_majors_listed(_results_card(first))) == RUNS_PER_PAGE
    assert len(_majors_listed(_results_card(second))) == 5
    assert "cert_page=2" in first


def test_nothing_is_silently_dropped(client, system):
    """It used to slice at 20 with nothing on the page saying so."""
    total = 25
    for _ in range(total):
        _run(system, 9)

    seen = 0
    for page in (1, 2, 3):
        seen += len(_majors_listed(_results_card(_page(client, system, cert_page=page))))

    assert seen == total


def test_paging_keeps_the_release_filter(client, system):
    """Otherwise page two of a filtered view quietly shows everything."""
    for _ in range(RUNS_PER_PAGE + 3):
        _run(system, 9)
    for _ in range(4):
        _run(system, 10)

    body = _page(client, system, cert_alma=9)
    assert "cert_alma=9" in body and "cert_page=2" in body

    second = _results_card(_page(client, system, cert_alma=9, cert_page=2))
    assert _majors_listed(second) == ["9.0"] * 3


def test_an_out_of_range_page_does_not_crash(client, system):
    for _ in range(3):
        _run(system, 9)

    body = _page(client, system, cert_page=99)

    assert "certification-results" in body


def test_the_card_says_how_many_there_are(client, system):
    """A paginated table has to say what it is a page of."""
    for _ in range(RUNS_PER_PAGE + 2):
        _run(system, 9)

    body = _page(client, system)

    assert re.search(r">\s*\(12\)\s*<", _results_card(body)), (
        "a bare '12' also matches a uuid fragment in the ten details links"
    )


# --- cost ---------------------------------------------------------------------


def test_the_query_count_does_not_grow_with_the_run_count(client, system):
    """The reason this is server-side. Before, the page cost about four queries per
    run; a browser-side filter would not have helped, because that cost is paid
    building the rows rather than showing them."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    for _ in range(RUNS_PER_PAGE):
        _run(system, 9)
    with CaptureQueriesContext(connection) as ctx:
        client.get(reverse("hardware:detail", args=[system.slug]))
    full_page = len(ctx)

    # Three times the runs, same page size.
    for _ in range(RUNS_PER_PAGE * 2):
        _run(system, 9)
    with CaptureQueriesContext(connection) as ctx:
        client.get(reverse("hardware:detail", args=[system.slug]))

    assert len(ctx) == full_page
