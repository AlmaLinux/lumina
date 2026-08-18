"""What a search engine is told about a page.

Three things, kept together because they answer one question - "what is this page, to somebody who
has not opened it" - and because a listing's description belongs in one place rather than in a
template, a sitemap and a JSON-LD block separately.

The structured data is built here rather than written into templates so it can be asserted against
directly. A JSON-LD block that quietly stops describing the page is the kind of thing nobody
notices for a year.
"""
from __future__ import annotations

from typing import Any

# Long enough to say something, short enough that a search result shows all of it. Google truncates
# around 155-160 characters; going past that buys nothing and risks the cut landing mid-word.
DESCRIPTION_LIMIT = 155


def clamp(text: str, limit: int = DESCRIPTION_LIMIT) -> str:
    """One line, no longer than a search result will show, cut at a word boundary."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(".,;:") + "…"


def listing_description(listing, releases: list[str] | None = None) -> str:
    """What a hardware listing is, for a reader who found it in a search result.

    Its own description where it has one, because somebody wrote that on purpose. Otherwise a
    sentence built from what the catalog knows, which is better than letting a search engine pick
    the first prose on the page - that would be the breadcrumb.
    """
    if (listing.description or "").strip():
        return clamp(listing.description)
    vendor = getattr(listing.vendor, "name", "") or ""
    on = ", ".join(releases or [])
    where = f" on AlmaLinux {on}" if on else " for AlmaLinux"
    return clamp(
        f"{vendor} {listing.name}: certified{where} in the AlmaLinux Certification Catalog.")


def product_ld(listing, url: str, description: str) -> dict[str, Any]:
    """A hardware listing as schema.org ``Product``.

    The type a certification catalog entry actually is, which is what earns a rich result rather
    than a plain link. Deliberately modest: name, brand, description, url. No ``offers`` and no
    ``aggregateRating`` - this catalog sells nothing and rates nothing, and inventing either is how
    structured data turns into a manual action.
    """
    data: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": listing.name,
        "description": description,
        "url": url,
    }
    vendor = getattr(listing.vendor, "name", "") or ""
    if vendor:
        data["brand"] = {"@type": "Brand", "name": vendor}
    return data


def software_ld(software, url: str, description: str) -> dict[str, Any]:
    """A software listing as ``SoftwareApplication``, which is what it is.

    ``operatingSystem`` is the point of the entry: the whole claim is "this runs on AlmaLinux".
    """
    data: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "SoftwareApplication",
        "name": software.name,
        "description": description,
        "url": url,
        "operatingSystem": "AlmaLinux",
    }
    vendor = getattr(software.vendor, "name", "") or ""
    if vendor:
        data["publisher"] = {"@type": "Organization", "name": vendor}
    return data


def item_list_ld(name: str, entries: list[tuple[str, str]]) -> dict[str, Any]:
    """A browse page as ``ItemList``: what is on it, in the order it is shown.

    ``entries`` is ``(name, absolute url)``. Positions are 1-based, which the schema requires.
    """
    return {
        "@context": "https://schema.org",
        "@type": "ItemList",
        "name": name,
        "numberOfItems": len(entries),
        "itemListElement": [
            {"@type": "ListItem", "position": index, "name": item_name, "url": item_url}
            for index, (item_name, item_url) in enumerate(entries, start=1)
        ],
    }
