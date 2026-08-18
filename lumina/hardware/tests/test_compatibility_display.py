"""Separating who asserted validation from what the community confirmed.

Hardware keeps both kinds of evidence in one table, ``CommunityAttestation``, told
apart only by ``level``. So a release with one vendor run and eight community runs
used to read "Vendor-validated / 9 attestations" - which credits the vendor with
the community's work and hides the community's contribution inside a number that
looks like part of the certification.

Software has the two in separate tables and presents them separately: each
certification gets its own badge, and the community count sits beside them. This
brings hardware to the same shape:

- **Certified by** lists one badge per *official* assertion - vendor, AlmaLinux, or
  both, since a release can hold both.
- **Community confirmations** counts only community-level attestations, so it is
  what the community actually did and nothing else.
- A release with no official assertion shows Community, matching software's
  fallback.
- A declared release with no evidence at all still says so.

The badges read "Vendor" / "AlmaLinux" / "Community" rather than carrying the
enum's "-validated" suffix: the column header is already "Certified by", so the
suffix restated it once per cell. See
``lumina.core.certification.SHORT_LABELS``.
"""
from __future__ import annotations

import re

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from lumina.core.certification import ValidationLevel
from lumina.hardware.models import (
    CommunityAttestation,
    ListingVersion,
    Submission,
    System,
)
from lumina.hardware.services import recompute_listing_levels
from lumina.releases.models import AlmaLinuxRelease
from lumina.vendors.models import Vendor

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture(autouse=True)
def releases():
    for major in (8, 9, 10):
        AlmaLinuxRelease.objects.get_or_create(
            major=major, defaults={"supported": True},
        )


@pytest.fixture
def system():
    vendor = Vendor.objects.create(name="Dell Inc.", published=True, verified=True)
    return System.objects.create(
        vendor=vendor, name="PowerEdge R750", published=True,
    )


def _cite(system, major, *, source=ListingVersion.SOURCE_RUN):
    return ListingVersion.objects.create(
        listing_system=system,
        release=AlmaLinuxRelease.objects.get(major=major),
        source=source,
    )


def _attest(version, system, level, who):
    """One attestation at a given tier, from a named person.

    Every attestation needs exactly one source; a submission is the cheaper of the
    two to build and what it is evidence *from* is not what these tests are about.
    """
    user = User.objects.create_user(who)
    return CommunityAttestation.objects.create(
        version=version, listing_system=system, level=level, attested_by=user,
        submission=Submission.objects.create(
            submitter=user, listing_system=system, claimed_validation_level=level,
        ),
    )


def _row(body: str, label: str) -> str:
    """The compatibility table row whose first cell is ``label``.

    Whitespace-tolerant, because the cell is no longer a one-liner: it can carry a "from 10.3"
    badge and a disclaimer under the release name. Matching ``>{label}<`` exactly found nothing
    the moment the template wrapped, which is a brittle helper rather than a real failure.

    Anchored on the label *as a cell's whole text* - ``>``, optional whitespace, the label,
    optional whitespace, then a tag - so it cannot land inside the disclaimer sentence, which
    contains the same release name in prose.
    """
    match = re.search(rf">\s*{re.escape(label)}\s*<", body)
    assert match, f"no compatibility row for {label!r}"
    start = body.rindex("<tr>", 0, match.start())
    return body[start:body.index("</tr>", match.start())]


def _page(client, system):
    return client.get(
        reverse("hardware:detail", args=[system.slug])
    ).content.decode()


def _badges(row: str) -> list[str]:
    """The tier badges in this row, in order, by their visible text.

    An exact list rather than substring checks. It subsumes the negative
    assertions - ``== ["Community"]`` already says the vendor badge is absent - and
    it cannot be fooled by the tier name appearing elsewhere in the row, which
    matters now that the badges are single words.
    """
    return [
        text.strip() for text in
        re.findall(r'class="badge badge-validation badge-\w+[^"]*">([^<]*)<', row)
    ]


# --- the split ----------------------------------------------------------------


def test_the_vendors_assertion_and_the_community_count_are_separate(client, system):
    """The case that motivated this: one vendor run, eight community runs.

    Before, that read as "Vendor-validated / 9". The 9 credited the vendor with the
    community's eight and buried the community's contribution in a number that
    looked like part of the certification.
    """
    nine = _cite(system, 9)
    _attest(nine, system, ValidationLevel.VENDOR, "dell-eng")
    for index in range(8):
        _attest(nine, system, ValidationLevel.COMMUNITY, f"fan{index}")
    recompute_listing_levels(system)

    row = _row(_page(client, system), "AlmaLinux 9")

    assert _badges(row) == ["Vendor"]
    assert "8 confirmations" in row
    # The vendor's own run is not one of the community's confirmations.
    assert "9 confirmations" not in row


def test_a_release_holding_both_official_tiers_shows_both(client, system):
    """A release can be certified by its vendor *and* by AlmaLinux. Reducing that
    to one badge throws away half of who validated it."""
    nine = _cite(system, 9)
    _attest(nine, system, ValidationLevel.VENDOR, "dell-eng")
    _attest(nine, system, ValidationLevel.ALMALINUX, "sig-member")
    recompute_listing_levels(system)

    row = _row(_page(client, system), "AlmaLinux 9")

    # Rank-descending, as ``official_levels()`` orders them.
    assert _badges(row) == ["Vendor", "AlmaLinux"]


def test_a_release_with_only_community_evidence_says_so(client, system):
    nine = _cite(system, 9)
    for index in range(3):
        _attest(nine, system, ValidationLevel.COMMUNITY, f"fan{index}")
    recompute_listing_levels(system)

    row = _row(_page(client, system), "AlmaLinux 9")

    assert _badges(row) == ["Community"]
    assert "3 confirmations" in row


def test_one_confirmation_is_not_pluralised(client, system):
    nine = _cite(system, 9)
    _attest(nine, system, ValidationLevel.COMMUNITY, "solo")
    recompute_listing_levels(system)

    row = _row(_page(client, system), "AlmaLinux 9")

    assert "1 confirmation" in row
    assert "1 confirmations" not in row


def test_an_officially_certified_release_with_no_community_evidence(client, system):
    """The vendor certified it and nobody else has run it yet. The count should read
    as empty rather than borrowing the vendor's attestation."""
    nine = _cite(system, 9)
    _attest(nine, system, ValidationLevel.VENDOR, "dell-eng")
    recompute_listing_levels(system)

    row = _row(_page(client, system), "AlmaLinux 9")

    assert _badges(row) == ["Vendor"]
    assert "none yet" in row


def test_a_declared_release_still_reads_as_declared(client, system):
    """No evidence of any kind. Distinct from "certified with no confirmations"."""
    _cite(system, 8, source=ListingVersion.SOURCE_DECLARED)

    row = _row(_page(client, system), "AlmaLinux 8")

    assert "Declared, not yet validated" in row
    assert _badges(row) == []


def test_a_declared_release_with_a_community_tier_says_both(client, system):
    """A tier and a provenance are two facts, and this row carries both.

    Accepting a manual submission records a community attestation against a declared
    release, so this is the ordinary state of a declared listing rather than a corner
    case. The marker used to be one arm of a fallback that only ran when the row had no
    tier at all, so the moment the attestation landed the row rendered as a bare
    "Community" badge, pixel-identical to a release a validation run had proven. The
    footnote lower down this page promising that unproven releases are marked was then
    dangling, and the API had the same hole in its own docstring.

    Nothing had been run on this release. The page has to say so.
    """
    eight = _cite(system, 8, source=ListingVersion.SOURCE_DECLARED)
    _attest(eight, system, ValidationLevel.COMMUNITY, "somebody")
    recompute_listing_levels(system)

    row = _row(_page(client, system), "AlmaLinux 8")

    assert _badges(row) == ["Community"]
    assert "Declared, not yet validated" in row


def test_the_abandonment_case_reads_correctly_across_releases(client, system):
    """The whole point, on one page: the vendor certified 8 and stopped, and the
    community carried 10."""
    eight = _cite(system, 8)
    _attest(eight, system, ValidationLevel.VENDOR, "dell-eng")
    ten = _cite(system, 10)
    for index in range(4):
        _attest(ten, system, ValidationLevel.COMMUNITY, f"fan{index}")
    recompute_listing_levels(system)

    body = _page(client, system)
    eight_row = _row(body, "AlmaLinux 8")
    ten_row = _row(body, "AlmaLinux 10")

    assert _badges(eight_row) == ["Vendor"]
    assert "none yet" in eight_row
    assert _badges(ten_row) == ["Community"]
    assert "4 confirmations" in ten_row


def test_the_header_counts_community_confirmations_only(client, system):
    """The badge beside it already says who certified it, so this is the other
    half. Counting the vendor's run here would make the header disagree with every
    row in the table."""
    nine = _cite(system, 9)
    _attest(nine, system, ValidationLevel.VENDOR, "dell-eng")
    for index in range(3):
        _attest(nine, system, ValidationLevel.COMMUNITY, f"fan{index}")
    ten = _cite(system, 10)
    _attest(ten, system, ValidationLevel.COMMUNITY, "ten-fan")
    recompute_listing_levels(system)

    body = _page(client, system)

    assert "4 community confirmations" in body
    assert "5 community confirmations" not in body


def test_the_release_table_costs_a_fixed_number_of_queries(client, system):
    """Both helpers walk each release's attestations, so without a prefetch the
    table is two extra queries per release - invisible with three releases and
    ugly with ten."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    for major in (8, 9, 10):
        row = _cite(system, major)
        for index in range(3):
            _attest(row, system, ValidationLevel.COMMUNITY, f"u{major}{index}")
    recompute_listing_levels(system)

    with CaptureQueriesContext(connection) as ctx:
        client.get(reverse("hardware:detail", args=[system.slug]))
    with_three = len(ctx)

    # A fourth release must not add queries.
    extra = ListingVersion.objects.create(
        listing_system=system,
        release=AlmaLinuxRelease.objects.get_or_create(
            major=11, defaults={"supported": True})[0],
    )
    _attest(extra, system, ValidationLevel.COMMUNITY, "eleven-fan")
    recompute_listing_levels(system)

    with CaptureQueriesContext(connection) as ctx:
        client.get(reverse("hardware:detail", args=[system.slug]))

    assert len(ctx) == with_three


# --- the API says the same thing ----------------------------------------------


def test_the_api_separates_them_too(client, system):
    """A client reading the JSON should not have to infer the split the page makes."""
    nine = _cite(system, 9)
    _attest(nine, system, ValidationLevel.VENDOR, "dell-eng")
    _attest(nine, system, ValidationLevel.ALMALINUX, "sig-member")
    for index in range(5):
        _attest(nine, system, ValidationLevel.COMMUNITY, f"fan{index}")
    recompute_listing_levels(system)

    payload = client.get(f"/api/v1/systems/{system.slug}/").json()
    row = next(r for r in payload["compatibility"] if r["major"] == 9)

    assert sorted(row["certifications"]) == ["almalinux", "vendor"]
    assert row["community_confirmations"] == 5
    # The existing total keeps its meaning rather than quietly changing.
    assert row["attestation_count"] == 7


def test_the_api_reports_no_certifications_for_a_community_only_release(
    client, system
):
    nine = _cite(system, 9)
    _attest(nine, system, ValidationLevel.COMMUNITY, "fan")
    recompute_listing_levels(system)

    payload = client.get(f"/api/v1/systems/{system.slug}/").json()
    row = next(r for r in payload["compatibility"] if r["major"] == 9)

    assert row["certifications"] == []
    assert row["community_confirmations"] == 1
    assert row["validation_level"] == ValidationLevel.COMMUNITY
