"""What the site tells a search engine about itself.

The catalog's whole public value is answering "is this machine certified for AlmaLinux", which is
a question people type into a search engine. None of this existed: no robots.txt, no sitemap, no
description, no canonical - and, most urgently, nothing stopping the staging copy being indexed
alongside production under near-identical content.
"""
from __future__ import annotations

import re

import pytest
from django.test import override_settings
from django.urls import reverse

from lumina.hardware.models import Component, ComponentKind, System
from lumina.software.models import Software
from lumina.vendors.models import Vendor

pytestmark = pytest.mark.django_db

INDEXED = override_settings(LUMINA_ALLOW_INDEXING=True)


def _meta(body: str, attr: str, value: str) -> str:
    """One meta tag's content. Asserted against the tag rather than the page, because a listing's
    name and its description are both rendered in the body too - a substring match anywhere would
    pass with no meta tag at all."""
    found = re.search(rf'<meta {attr}="{re.escape(value)}" content="([^"]*)"', body)
    return found.group(1) if found else ""


@pytest.fixture
def vendor():
    return Vendor.objects.create(name="SEO Vendor", slug="seo-vendor", published=True)


@pytest.fixture
def system(vendor):
    return System.objects.create(
        vendor=vendor, name="PowerEdge SEO", slug="seo-poweredge", published=True)


# --- keeping staging out of the index ---------------------------------------------


def test_a_deployment_is_not_indexable_unless_it_says_so(client):
    """The default has to be the safe one: staging and production deploy from the same role, and
    only one of them should be findable."""
    response = client.get(reverse("core:home"))

    assert response.headers["X-Robots-Tag"] == "noindex, nofollow"


def test_the_production_deployment_carries_no_such_header(client):
    with INDEXED:
        response = client.get(reverse("core:home"))

    assert "X-Robots-Tag" not in response.headers


def test_a_deployment_that_is_not_indexable_refuses_every_path(client):
    """robots.txt asks a crawler not to fetch. The header above is what keeps a URL out of an
    index when something else links to it; this is the half that saves the crawl."""
    body = client.get("/robots.txt").content.decode()

    assert body.splitlines() == ["User-agent: *", "Disallow: /"]


def test_the_header_covers_more_than_html(client):
    """A crawler indexes JSON and PDFs too, and a listing's bundle is neither HTML nor private."""
    response = client.get("/robots.txt")

    assert response.headers["X-Robots-Tag"] == "noindex, nofollow"


# --- robots.txt where indexing is allowed -----------------------------------------


def test_robots_points_at_the_sitemap(client):
    with INDEXED:
        body = client.get("/robots.txt").content.decode()

    assert "Sitemap: http://testserver/sitemap.xml" in body


def test_robots_keeps_crawlers_out_of_what_needs_a_login(client):
    """Every one of these answers a crawler with a redirect: a wasted fetch that teaches it
    nothing."""
    with INDEXED:
        body = client.get("/robots.txt").content.decode()

    for prefix in ("/admin/", "/api/", "/my/", "/review/", "/submit/", "/oidc/"):
        assert f"Disallow: {prefix}" in body


def test_robots_does_not_disallow_the_catalog(client):
    with INDEXED:
        body = client.get("/robots.txt").content.decode()

    assert "Disallow: /hardware/" not in body
    assert "Disallow: /software/" not in body


# --- the sitemap ------------------------------------------------------------------


def test_the_sitemap_lists_a_published_listing(client, system):
    with INDEXED:
        body = client.get("/sitemap-systems.xml").content.decode()

    assert system.get_absolute_url() in body


def test_the_sitemap_leaves_out_what_is_not_published(client, vendor):
    """A draft is a 404 to everyone but its owner, so listing it advertises a URL that errors."""
    System.objects.create(vendor=vendor, name="Draft Box", slug="seo-draft", published=False)

    with INDEXED:
        body = client.get("/sitemap-systems.xml").content.decode()

    assert "seo-draft" not in body


def test_the_sitemap_index_names_every_section(client):
    with INDEXED:
        body = client.get(reverse("core:sitemap-index")).content.decode()

    for section in ("static", "systems", "components", "software"):
        assert f"sitemap-{section}.xml" in body


def test_the_sitemap_covers_components_and_software(client, vendor):
    Component.objects.create(vendor=vendor, name="SEO Card", slug="seo-card",
                             kind=ComponentKind.gpu.value, published=True)
    Software.objects.create(vendor=vendor, name="SEO App", slug="seo-app", published=True)

    with INDEXED:
        components = client.get("/sitemap-components.xml").content.decode()
        software = client.get("/sitemap-software.xml").content.decode()

    assert "seo-card" in components
    assert "seo-app" in software


def test_the_browse_pages_are_in_the_sitemap(client):
    """They are the entry points a crawler works down from."""
    with INDEXED:
        body = client.get("/sitemap-static.xml").content.decode()

    for url in (reverse("hardware:systems"), reverse("hardware:components"),
                reverse("software:browse")):
        assert url in body


# --- what a result and a shared link say ------------------------------------------


def test_a_listing_page_describes_itself(client, system):
    """Without this a search engine writes its own summary from the first prose on the page,
    which on every page here is the breadcrumb."""
    body = client.get(system.get_absolute_url()).content.decode()

    assert _meta(body, "name", "description").startswith("SEO Vendor PowerEdge SEO: certified")


def test_a_listings_own_words_win(client, vendor):
    """Somebody wrote that on purpose."""
    listing = System.objects.create(
        vendor=vendor, name="Described Box", slug="seo-described", published=True,
        description="A 2U dual-socket rack server for dense virtualisation.")

    body = client.get(listing.get_absolute_url()).content.decode()

    assert _meta(body, "name", "description") == (
        "A 2U dual-socket rack server for dense virtualisation.")


def test_every_page_declares_a_canonical_without_its_filters(client):
    """Faceted browse multiplies into a very large number of URLs over near-identical content."""
    body = client.get(
        reverse("hardware:systems"), {"vendor": "seo-vendor", "page": "3"}
    ).content.decode()

    assert '<link rel="canonical" href="http://testserver/hardware/systems/">' in body


def test_a_listing_page_carries_structured_data(client, system):
    """A Product block is what earns a rich result rather than a plain blue link."""
    body = client.get(system.get_absolute_url()).content.decode()

    assert 'type="application/ld+json"' in body
    assert '"@type":"Product"' in body
    assert '"name":"PowerEdge SEO"' in body


def test_software_is_described_as_software(client, vendor):
    """``SoftwareApplication`` with the operating system named, because the whole claim the entry
    makes is that it runs on AlmaLinux."""
    Software.objects.create(vendor=vendor, name="SEO App", slug="seo-app", published=True)

    body = client.get(reverse("software:detail", args=["seo-app"])).content.decode()

    assert '"@type":"SoftwareApplication"' in body
    assert '"operatingSystem":"AlmaLinux"' in body


def test_a_browse_page_lists_what_is_on_it(client, system):
    body = client.get(reverse("hardware:systems")).content.decode()

    assert '"@type":"ItemList"' in body
    assert '"@type":"ListItem"' in body


def test_structured_data_cannot_break_out_of_its_script(client, vendor):
    """A listing name is somebody else's text. Unescaped, ``</script>`` in one ends the block
    early and everything after it is markup the page did not mean to emit."""
    System.objects.create(
        vendor=vendor, name="Bad </script><b>x</b> Box", slug="seo-escape", published=True)

    body = client.get(reverse("hardware:detail", args=["seo-escape"])).content.decode()

    assert "</script><b>x</b>" not in body
    assert "\\u003C/script\\u003E" in body or "\\u003c/script\\u003e" in body


def test_a_shared_link_carries_a_card(client, system):
    body = client.get(system.get_absolute_url()).content.decode()

    assert _meta(body, "property", "og:title") == "PowerEdge SEO - AlmaLinux Certification Catalog"
    assert _meta(body, "property", "og:url") == "http://testserver/hardware/seo-poweredge/"
    assert _meta(body, "property", "og:description").startswith("SEO Vendor PowerEdge SEO")
    assert 'name="twitter:card"' in body


def test_the_title_and_the_card_say_the_same_thing(client, system):
    """Three template blocks would drift; one value from the view cannot."""
    body = client.get(system.get_absolute_url()).content.decode()

    assert "<title>PowerEdge SEO - AlmaLinux Certification Catalog</title>" in body
    assert _meta(body, "property", "og:title") == "PowerEdge SEO - AlmaLinux Certification Catalog"


def test_a_page_that_sets_nothing_still_describes_the_site(client):
    """The fallback has to be a real sentence, not an empty attribute."""
    body = client.get(reverse("core:home")).content.decode()

    assert _meta(body, "name", "description").startswith("Hardware and software verified")


def test_the_footer_names_the_software_behind_the_catalog(client):
    """Asked for: a "powered by" credit linking to the project. Both layouts carry it - the
    public catalog and the signed-in pages are the same application, and a reader on either
    should be able to find what produced the page, or file a bug about it."""
    body = client.get(reverse("hardware:systems")).content.decode()

    assert "Powered by" in body
    assert "https://github.com/AlmaLinux/lumina" in body
    assert "AlmaLinux Lumina" in body


def test_the_credit_opens_the_project_beside_the_catalog(client):
    """It leaves the site, so it opens in a new tab rather than replacing the page somebody was
    reading. ``noopener`` with it: a ``_blank`` target otherwise hands the opened page a handle
    on this one."""
    body = client.get(reverse("hardware:systems")).content.decode()

    link = body[body.index("https://github.com/AlmaLinux/lumina"):]
    link = link[:link.index("</a>")]
    assert 'target="_blank"' in link
    assert "noopener" in link


def test_the_signed_in_pages_carry_the_credit_too(client):
    """The two layouts are the same application. A reader on the dashboard has as much reason
    to find the project as one on a public listing."""
    from django.contrib.auth.models import User

    client.force_login(User.objects.create_user("footer-user", password="pw"))

    body = client.get(reverse("accounts:dashboard")).content.decode()

    assert "Powered by" in body
    assert "https://github.com/AlmaLinux/lumina" in body
