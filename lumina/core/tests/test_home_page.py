"""The home page covering both catalogs, not just hardware.

It was written when hardware was the whole site: the title said "AlmaLinux
Hardware Certification Catalog", the only two feeds were test runs and benchmarks,
and there was no way to reach the software catalog from it at all.

The two software feeds are ordered by **timestamp**, not by total, which is the
part worth pinning. A "most confirmed products" list settles permanently on
whatever got popular first, so a site with heavy activity looks idle. Ordering by
the newest confirmation makes the block turn over as people use it. See
``test_the_confirmation_feed_is_ordered_by_recency_not_popularity``, which fails
if anyone re-sorts it by count.
"""
from __future__ import annotations

import re
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from lumina.core.certification import ValidationLevel
from lumina.releases.models import AlmaLinuxRelease
from lumina.software.models import (
    Software,
    SoftwareAttestation,
    SoftwareCertification,
    SoftwareCompatibility,
)
from lumina.vendors.models import Vendor

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture(autouse=True)
def releases():
    for major in (8, 9, 10):
        AlmaLinuxRelease.objects.get_or_create(
            major=major, defaults={"supported": True},
        )


def _product(name, *, published=True):
    vendor, _ = Vendor.objects.get_or_create(
        name=f"{name} Ltd", defaults={"published": True, "scope": Vendor.SCOPE_SOFTWARE},
    )
    return Software.objects.create(vendor=vendor, name=name, published=published)


def _cite(product, major, *, status=SoftwareCompatibility.STATUS_APPROVED):
    return SoftwareCompatibility.objects.create(
        software=product,
        release=AlmaLinuxRelease.objects.get(major=major),
        status=status,
    )


def _certify(row, level=ValidationLevel.VENDOR, *, ago=None):
    certification = SoftwareCertification.objects.create(compatibility=row, level=level)
    if ago is not None:
        # ``certified_at`` is auto_now_add, so ordering has to be forced by update.
        SoftwareCertification.objects.filter(pk=certification.pk).update(
            certified_at=timezone.now() - ago
        )
    row.software.recompute_levels()
    return certification


def _confirm(row, who, *, ago=None):
    user = User.objects.create_user(who)
    attestation = SoftwareAttestation.objects.create(compatibility=row, user=user)
    if ago is not None:
        SoftwareAttestation.objects.filter(pk=attestation.pk).update(
            created_at=timezone.now() - ago
        )
    return attestation


def _home(client):
    return client.get(reverse("core:home")).content.decode()


def _names(response_context, key):
    return [product.name for product in response_context[key]]


# --- the page is no longer hardware-only --------------------------------------


def test_the_page_links_to_the_software_catalog(client):
    """There was no route from the home page's own content into the catalog.

    Asserted on the button's label, not on ``reverse("software:browse")``.
    ``base_public.html`` has always carried a "Software" link in the site-wide
    navbar, so a bare URL assertion passes on every page of the site and would
    have said nothing about this page - confirmed by deleting the button and
    watching the test still pass.
    """
    body = _home(client)

    assert "Browse software</a>" in body, "no browse-software control on the page"


def test_the_feed_cards_are_in_the_expected_order(client):
    """Hardware on the top line, software on the bottom, reading left to right.

    The grid is four ``col-lg-6`` cards in one row, so DOM order is the only thing
    deciding layout, and reordering them is a silent one-line move with nothing
    else to catch it.

    Needs one piece of content: the whole row is omitted on a bare site, so an
    empty page would compare an empty list against an empty list and pass.
    """
    _certify(_cite(_product("Anything"), 9))

    body = _home(client)
    titles = re.findall(r'class="h5 card-title mb-0">([^<]+)<', body)

    # "hardware" rather than "systems": the card carries component claims too now, and a scoped
    # run named after a card under a heading promising systems reads as a certified machine.
    assert titles == [
        "Recently validated hardware", "Latest benchmark results",
        "Recently validated software", "Recent community confirmations",
    ], titles


def test_each_software_feed_links_to_the_full_catalog(client):
    """So a full list is one click from any row.

    Needs data: the feed cards live inside the row that an empty site omits
    entirely, so asserting this on a bare page can never pass.
    """
    row = _cite(_product("Linked"), 9)
    _certify(row)
    _confirm(row, "fan")

    body = _home(client)

    assert body.count("Browse all</a>") == 2


def test_the_title_does_not_claim_to_be_a_hardware_catalog(client):
    body = _home(client)

    assert "Hardware Certification Catalog" not in body
    assert "AlmaLinux Certification Catalog" in body


def test_the_tier_descriptions_are_not_hardware_only(client):
    """Each tier is earned differently in the two catalogs - a suite run for
    hardware, a click for software - so copy describing only test results
    described only half the site."""
    body = _home(client)

    assert "on reference hardware." not in body
    assert "verified hardware vendor" not in body


# --- recently validated software ----------------------------------------------


def test_recently_validated_software_is_listed_newest_first(client):
    old = _product("Elderly")
    _certify(_cite(old, 9), ago=timedelta(days=30))
    fresh = _product("Recent")
    _certify(_cite(fresh, 10), ago=timedelta(hours=1))

    response = client.get(reverse("core:home"))

    assert _names(response.context, "recent_software") == ["Recent", "Elderly"]


def test_a_product_certified_on_several_releases_appears_once(client):
    """A vendor certifying 8, 9, and 10 in one sitting would otherwise fill the
    entire feed with one product and read as three unrelated events."""
    product = _product("Broadly")
    for major in (8, 9, 10):
        _certify(_cite(product, major))
    _certify(_cite(_product("Other"), 9), ago=timedelta(days=1))

    response = client.get(reverse("core:home"))

    assert _names(response.context, "recent_software") == ["Broadly", "Other"]


def test_an_uncertified_product_is_not_in_the_validated_feed(client):
    """Cited but never certified by anyone official. It belongs in the catalog,
    not in a feed of validations."""
    _cite(_product("Merely Cited"), 9)

    response = client.get(reverse("core:home"))

    assert _names(response.context, "recent_software") == []


def test_an_unpublished_product_is_never_in_a_feed(client):
    product = _product("Draft", published=False)
    row = _cite(product, 9)
    _certify(row)
    _confirm(row, "someone")

    response = client.get(reverse("core:home"))

    assert _names(response.context, "recent_software") == []
    assert _names(response.context, "recent_confirmations") == []


def test_a_pending_release_does_not_reach_either_feed(client):
    """A pending row is one person's unreviewed claim about somebody else's
    product. The home page is the last place it should surface."""
    product = _product("Unreviewed")
    row = _cite(product, 9, status=SoftwareCompatibility.STATUS_PENDING)
    _certify(row)
    _confirm(row, "eager")

    response = client.get(reverse("core:home"))

    assert _names(response.context, "recent_software") == []
    assert _names(response.context, "recent_confirmations") == []


# --- recent community confirmations -------------------------------------------


def test_recent_confirmations_are_listed_newest_first(client):
    stale = _product("Stale")
    _confirm(_cite(stale, 9), "old-fan", ago=timedelta(days=14))
    lively = _product("Lively")
    _confirm(_cite(lively, 10), "new-fan", ago=timedelta(minutes=5))

    response = client.get(reverse("core:home"))

    assert _names(response.context, "recent_confirmations") == ["Lively", "Stale"]


def test_the_confirmation_feed_is_ordered_by_recency_not_popularity(client):
    """The requirement that the block stays dynamic.

    Sorting by total confirmations would pin the same products at the top forever,
    so a site with heavy activity would look idle. Popular-but-quiet loses to
    unpopular-but-active here on purpose.
    """
    popular = _product("Popular")
    popular_row = _cite(popular, 9)
    for index in range(8):
        _confirm(popular_row, f"crowd{index}", ago=timedelta(days=20))
    quiet = _product("JustConfirmed")
    _confirm(_cite(quiet, 9), "solo", ago=timedelta(minutes=1))

    response = client.get(reverse("core:home"))

    assert _names(response.context, "recent_confirmations")[0] == "JustConfirmed"


def test_the_confirmation_count_is_the_products_total(client):
    product = _product("Counted")
    row = _cite(product, 9)
    for index in range(3):
        _confirm(row, f"fan{index}")

    response = client.get(reverse("core:home"))

    assert response.context["recent_confirmations"][0].confirmations == 3
    assert "3 confirmations" in response.content.decode()


def test_confirmations_across_releases_are_totalled_not_multiplied(client):
    """Two aggregates over one join. Over two *different* multi-valued relations
    Django would fan the join out and inflate the count - here it is 3, not 9."""
    product = _product("Multi")
    for major, who in ((8, "a"), (9, "b"), (10, "c")):
        _confirm(_cite(product, major), who)

    response = client.get(reverse("core:home"))

    assert response.context["recent_confirmations"][0].confirmations == 3


def test_one_confirmation_is_not_pluralised(client):
    _confirm(_cite(_product("Solo"), 9), "only-fan")

    body = _home(client)

    assert "1 confirmation " in body or "1 confirmation<" in body
    assert "1 confirmations" not in body


# --- cost ---------------------------------------------------------------------


def test_the_feeds_do_not_add_a_query_per_product(client):
    """This is the busiest page on the site. The vendor name and the tier badge are
    both rendered per row, so without ``select_related`` each row would go back to
    the database for its vendor."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    for index in range(2):
        product = _product(f"Small{index}")
        row = _cite(product, 9)
        _certify(row)
        _confirm(row, f"fan-small-{index}")
    with CaptureQueriesContext(connection) as ctx:
        client.get(reverse("core:home"))
    baseline = len(ctx)

    for index in range(6):
        product = _product(f"Big{index}")
        row = _cite(product, 10)
        _certify(row)
        _confirm(row, f"fan-big-{index}")
    with CaptureQueriesContext(connection) as ctx:
        client.get(reverse("core:home"))

    assert len(ctx) == baseline, f"{baseline} -> {len(ctx)} queries"


def test_a_feed_with_nothing_in_it_says_so(client):
    """A card that renders empty beats a card that disappears: absent reads as
    "this site has no such feature" rather than "nobody has done it yet".

    Reached by giving one software feed content and not the other, which is the
    ordinary early state - products get certified before anyone confirms them.
    """
    _certify(_cite(_product("Certified Only"), 9))

    body = _home(client)

    assert "Certified Only" in body
    assert "No confirmations yet." in body


def test_the_other_feed_empties_the_same_way(client):
    _confirm(_cite(_product("Confirmed Only"), 9), "fan")

    body = _home(client)

    assert "Confirmed Only" in body
    assert "No software validated yet." in body


def test_a_completely_empty_site_renders_without_the_feeds(client):
    """Pre-existing deliberate behavior, kept: with nothing published at all the
    whole feed row is omitted rather than showing four "nothing yet" boxes."""
    response = client.get(reverse("core:home"))

    assert response.status_code == 200
    assert "No software validated yet." not in response.content.decode()


# --- running the suite -----------------------------------------------------------------
#
# The page's other audience. Everything else on it answers "what is in the catalog"; nothing
# answered "how do I get my machine into it", even though the community tier card promises that
# hardware certification is backed by certification-suite results.


def test_the_front_page_says_how_to_run_the_suite(client):
    body = _home(client)

    assert "Certify your own hardware" in body
    assert "sudo dnf -y install alma-cert &amp;&amp; sudo alma-cert run" in body


def test_the_command_is_selectable_in_one_click(client):
    """``user-select-all`` rather than a copy button. It is this project's existing affordance for
    a value the reader has to take away, and it cannot be broken by a script that did not load."""
    body = _home(client)

    assert 'class="user-select-all"' in body


def test_it_says_publishing_needs_an_account(client):
    """Running the suite needs nothing from us; publishing the bundle needs an account on both the
    API and the web path. Saying so here is cheaper than letting somebody find out after the run."""
    body = " ".join(_home(client).split())

    assert "free account" in body


def test_it_is_there_on_an_empty_site(client):
    """The feed row below is omitted entirely when nothing is published, and a fresh deployment is
    exactly when running the suite is the only useful thing a visitor can do here."""
    from lumina.results.models import TestRun

    assert not TestRun.objects.public().exists(), "the premise: nothing published"

    assert "Certify your own hardware" in _home(client)
