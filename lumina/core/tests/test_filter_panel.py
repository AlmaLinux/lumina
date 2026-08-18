"""The vendor facet in the catalog sidebar, shared by both browse pages.

``templates/catalog/_filter_panel.html`` renders for hardware systems, hardware
components, and software, so a change for one catalog lands on all three.

The vendor list is the only facet with no upper bound. Taxonomy values and
AlmaLinux releases are curated; vendors grow with the catalog and are expected to
run to thousands. So the block is a **window**: about five rows tall, scrollable,
capped at ``VENDOR_FACET_LIMIT`` rendered checkboxes, with a search box that goes
to the *server* to reach the rest.

Two correctness properties matter more than the sizing:

- A **selected** vendor is always rendered, however unpopular. Otherwise filtering
  by a vendor outside the window gives a page with the filter applied, nothing
  saying so, and no checkbox to switch it off.
- Search is server-side. Filtering only the rendered window in the browser would
  answer "no such vendor" for most of the catalog - worse than having no search.

Flat functions, because a flat ``def`` cannot fall out of collection whatever
``python_classes`` is narrowed to. (The class-based tests in
``hardware/tests/test_browse_views.py`` *are* collected - all five classes there end
in ``Tests``, which ``pyproject.toml`` matches. The trap is a class named ``TestFoo``,
which nothing here uses.)
"""
from __future__ import annotations

import re

import pytest
from django.urls import reverse

from lumina.hardware.models import Component, ComponentKind, System
from lumina.software.models import Software
from lumina.vendors.models import Vendor
from lumina.vendors.services import VENDOR_FACET_LIMIT, vendor_facet

pytestmark = pytest.mark.django_db

# Comfortably past the window, so truncation is exercised rather than approached.
_VENDOR_COUNT = VENDOR_FACET_LIMIT + 7

# The fragment endpoints answer HTMX only; a plain request is redirected to the
# catalog, so every fragment fetch here has to say it is HTMX.
_HX = {"HX-Request": "true"}


@pytest.fixture
def many_vendors():
    """Vendors with a deliberately uneven number of listings each.

    The facet orders by listing count, so a flat distribution would let an
    alphabetical implementation pass by accident.
    """
    vendors = []
    for index in range(_VENDOR_COUNT):
        vendor = Vendor.objects.create(
            name=f"Vendor {index:02d}", published=True, scope=Vendor.SCOPE_BOTH,
        )
        vendors.append(vendor)
        # Vendor 00 gets the most listings, and so on down. All three kinds, so the
        # components page has a vendor card to render at all.
        for copy in range(max(1, _VENDOR_COUNT - index)):
            System.objects.create(
                vendor=vendor, name=f"Box {index:02d}-{copy}", published=True,
            )
            Component.objects.create(
                vendor=vendor, name=f"NIC {index:02d}-{copy}", published=True,
                kind=ComponentKind.nic.value,
            )
            Software.objects.create(
                vendor=vendor, name=f"App {index:02d}-{copy}", published=True,
            )
    return vendors


def _vendor_block(body: str) -> str:
    """The options container, from its opening tag so its classes are in range."""
    anchor = body.index('id="vendor-options"')
    start = body.rindex("<div", 0, anchor)
    return body[start:body.index("</div>", anchor)]


# --- the window --------------------------------------------------------------


@pytest.mark.parametrize(
    "url_name", ["hardware:systems", "hardware:components", "software:browse"]
)
def test_the_vendor_block_is_height_capped_on_every_catalog(
    client, many_vendors, url_name
):
    """One partial serves three pages, so the cap has to hold on all of them."""
    body = client.get(reverse(url_name)).content.decode()

    assert "filter-values-scroll" in _vendor_block(body)


def test_only_a_window_of_vendors_is_rendered(client, many_vendors):
    """The point of the change: a thousand-vendor catalog must not put a
    thousand checkboxes into every page."""
    body = client.get(reverse("software:browse")).content.decode()

    rendered = _vendor_block(body).count('name="vendor"')
    assert rendered == VENDOR_FACET_LIMIT


def test_the_window_says_what_it_is_not_showing(client, many_vendors):
    """A silently truncated filter list reads as "these are all the vendors"."""
    body = client.get(reverse("software:browse")).content.decode()

    assert f"Showing {VENDOR_FACET_LIMIT} of {_VENDOR_COUNT}" in body


def test_the_window_holds_the_most_used_vendors(many_vendors):
    """Ordered by listing count, so a 25-row window is worth reading. Alphabetical
    would make its contents an accident of spelling."""
    facet = vendor_facet(Software)

    assert facet.matched == _VENDOR_COUNT
    assert facet.pool == _VENDOR_COUNT
    assert [v.name for v in facet.vendors[:3]] == [
        "Vendor 00", "Vendor 01", "Vendor 02",
    ]
    assert "Vendor 31" not in [v.name for v in facet.vendors]


def test_a_search_narrows_matched_but_not_the_pool(many_vendors):
    """The two counts answer different questions, and conflating them is what hid
    the search box the moment a term got specific enough to be useful."""
    facet = vendor_facet(Software, query="Vendor 31")

    assert facet.matched == 1
    assert facet.pool == _VENDOR_COUNT


def test_a_short_vendor_list_is_untouched(client):
    """Two vendors need no search box, and ``max-height`` does nothing until the
    content exceeds it, so nothing about a small catalog changes."""
    for name in ("Dell Inc.", "HPE"):
        vendor = Vendor.objects.create(name=name, published=True)
        System.objects.create(vendor=vendor, name=f"Box {name}", published=True)

    body = client.get(reverse("hardware:systems")).content.decode()

    assert 'name="vendor_q"' not in body
    assert "Showing" not in _vendor_block(body)
    assert body.count('name="vendor"') == 2


def test_each_catalog_offers_only_its_own_vendors(client):
    """Unchanged by the windowing: /systems/ must not advertise a vendor that only
    ever made components."""
    board_only = Vendor.objects.create(name="ASRock", published=True)
    Component.objects.create(
        vendor=board_only, name="B650M", kind=ComponentKind.motherboard.value,
        published=True,
    )
    system_only = Vendor.objects.create(name="Dell Inc.", published=True)
    System.objects.create(vendor=system_only, name="R750", published=True)

    systems = client.get(reverse("hardware:systems")).content.decode()
    components = client.get(reverse("hardware:components")).content.decode()

    assert 'value="dell-inc"' in _vendor_block(systems)
    assert 'value="asrock"' not in _vendor_block(systems)
    assert 'value="asrock"' in _vendor_block(components)


# --- the trap the window creates ---------------------------------------------


def test_a_selected_vendor_outside_the_window_is_still_rendered(
    client, many_vendors
):
    """Otherwise the filter is applied with no checkbox on screen to clear it.

    Vendor 31 has the fewest listings, so it sorts last and falls outside the
    window - exactly the case that would strand a user.
    """
    stranded = many_vendors[-1]
    assert stranded.name == f"Vendor {_VENDOR_COUNT - 1:02d}"

    body = client.get(
        reverse("software:browse"), {"vendor": stranded.slug}
    ).content.decode()

    block = _vendor_block(body)
    assert f'value="{stranded.slug}"' in block
    assert "checked" in block


def test_the_selected_vendor_still_filters_the_results(client, many_vendors):
    """The checkbox being present is only half of it - the filter has to work."""
    chosen = many_vendors[-1]

    body = client.get(
        reverse("software:browse"), {"vendor": chosen.slug}
    ).content.decode()

    assert "Showing 1 product" in body


# --- server-side search ------------------------------------------------------


def test_a_long_vendor_list_gets_a_search_box(client, many_vendors):
    body = client.get(reverse("software:browse")).content.decode()

    assert 'name="vendor_q"' in body
    assert reverse("software:vendor_search") in body


def test_the_vendor_search_does_not_leak_into_the_product_search(
    client, many_vendors
):
    """Both inputs sit inside the same filter form. Named ``q``, the vendor term
    would be submitted as the catalog's own search and quietly empty the results.

    Asserted behaviourally rather than by counting attributes, because the page has
    a legitimate second ``q`` - the panel's hidden field carrying the product
    search across filter changes.
    """
    unfiltered = client.get(reverse("software:browse")).content.decode()
    searched = client.get(
        reverse("software:browse"), {"vendor_q": "Vendor 31"}
    ).content.decode()

    count = re.search(r"Showing (\d+) product", unfiltered).group(1)
    # The vendor term narrowed the vendor *list* and left the catalog alone.
    assert f"Showing {count} product" in searched
    assert int(count) > 1
    assert 'value="vendor-31"' in _vendor_block(searched)
    assert 'value="vendor-00"' not in _vendor_block(searched)


@pytest.mark.parametrize(
    "url_name,args",
    [("software:vendor_search", []), ("hardware:vendor_search", ["systems"])],
)
def test_searching_reaches_vendors_outside_the_window(
    client, many_vendors, url_name, args
):
    """The reason it is server-side. Vendor 31 is not in the rendered window, and a
    browser-side filter could never find it."""
    response = client.get(
        reverse(url_name, args=args), {"vendor_q": "31"}, headers=_HX,
    )

    body = response.content.decode()
    assert response.status_code == 200
    assert 'value="vendor-31"' in body
    assert 'value="vendor-00"' not in body


def test_searching_returns_only_the_options_fragment(client, many_vendors):
    """It swaps into the panel, so it must not carry a whole page with it."""
    body = client.get(
        reverse("software:vendor_search"),
        {"vendor_q": "vendor"}, headers=_HX,
    ).content.decode()

    assert 'id="vendor-options"' in body
    assert "<html" not in body.lower()


@pytest.mark.parametrize(
    "url_name,args,destination",
    [
        ("software:vendor_search", [], "software:browse"),
        ("hardware:vendor_search", ["systems"], "hardware:systems"),
    ],
)
def test_a_plain_request_for_the_fragment_goes_to_the_catalog(
    client, many_vendors, url_name, args, destination
):
    """Reached as a normal navigation, this URL must not render a bare fragment.

    That is what a browser did on Enter: the search box sits inside the filter
    form, so the keypress submitted it, and landing on this endpoint painted the
    options partial - complete with "No vendor matches that name" - as the whole
    document. A fragment is only ever an answer to HTMX.
    """
    response = client.get(reverse(url_name, args=args), {"vendor_q": "zzzznope"})

    assert response.status_code == 302
    assert response["Location"].startswith(reverse(destination))
    # The term survives the bounce, so the page it lands on shows what was typed.
    assert "vendor_q=zzzznope" in response["Location"]


def test_the_bounce_keeps_the_filters_that_were_applied(client, many_vendors):
    """Otherwise pressing Enter would quietly clear the user's other filters."""
    chosen = many_vendors[0]

    response = client.get(
        reverse("software:vendor_search"),
        {"vendor_q": "vendor", "vendor": chosen.slug, "license": "commercial"},
    )

    location = response["Location"]
    assert f"vendor={chosen.slug}" in location
    assert "license=commercial" in location


def test_a_search_matching_nothing_says_so(client, many_vendors):
    body = client.get(
        reverse("software:vendor_search"), {"vendor_q": "zzzznope"}, headers=_HX,
    ).content.decode()

    assert "No vendor matches that name" in body


def test_a_search_keeps_the_current_selection_checked(client, many_vendors):
    """The swap replaces the checkboxes, so it has to carry their state or
    searching would silently drop the filter you already applied."""
    chosen = many_vendors[0]

    body = client.get(
        reverse("software:vendor_search"),
        {"vendor_q": chosen.name, "vendor": chosen.slug},
        headers=_HX,
    ).content.decode()

    assert 'value="vendor-00"' in body
    assert "checked" in body


def test_a_search_that_matched_nothing_keeps_its_own_box(client, many_vendors):
    """The third place this same mistake could hide.

    Gated on the rendered list, an empty result took the whole vendor card with it,
    search box and all - so the term that emptied it could not be cleared. Gated on
    the pool, the card stays and reports the empty state.
    """
    body = client.get(
        reverse("software:browse"), {"vendor_q": "zzzznope"}
    ).content.decode()

    assert "No vendor matches that name" in body
    assert 'name="vendor_q"' in body
    assert 'value="zzzznope"' in body


def test_the_search_term_survives_a_page_reload(client, many_vendors):
    """It rides along in the query string, so a reloaded page keeps the narrowed
    list rather than snapping back to the top 25."""
    body = client.get(
        reverse("software:browse"), {"vendor_q": "31"}
    ).content.decode()

    block = _vendor_block(body)
    assert 'value="vendor-31"' in block
    assert 'value="vendor-00"' not in block
    assert 'value="31"' in body  # the input keeps what was typed


def test_the_vendor_search_term_is_not_treated_as_a_category(client, many_vendors):
    """Unreserved, ``vendor_q`` would be offered to ``apply_category_filters`` as a
    candidate category slug."""
    from lumina.hardware.filters import _RESERVED_PARAMS as HARDWARE
    from lumina.software.filters import _RESERVED_PARAMS as SOFTWARE

    assert "vendor_q" in HARDWARE
    assert "vendor_q" in SOFTWARE
