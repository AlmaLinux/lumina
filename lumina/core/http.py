"""Small HTTP helpers shared across the apps."""
from __future__ import annotations

from django.http import HttpRequest, HttpResponseRedirect
from django.urls import reverse


def redirect_preserving_query(
    request: HttpRequest, url_name: str, *args
) -> HttpResponseRedirect:
    """Send the browser to ``url_name`` carrying this request's query string.

    Used by the HTMX fragment endpoints when a request arrives without
    ``HX-Request``. A partial answered to a top-level navigation renders as the
    whole document, which is how pressing Enter in the vendor search box - an
    input that lives inside the filter form - could paint "No vendor matches that
    name" as an entire page.

    The query string comes along so the page it lands on shows the same filters and
    the same search term rather than resetting to an unfiltered catalog.
    """
    target = reverse(url_name, args=args)
    query = request.META.get("QUERY_STRING", "")
    return HttpResponseRedirect(f"{target}?{query}" if query else target)


def params(request: HttpRequest) -> dict[str, list[str]]:
    """The querystring as ``{key: [values]}``, repeated keys preserved.

    ``lists()`` rather than ``dict(request.GET)``, which keeps only the last value
    of a repeated key - and repeated keys are how every facet filter is expressed
    (``?vendor=dell&vendor=hpe``). Both catalogs had their own identical one-liner.
    """
    return dict(request.GET.lists())


def vendor_facet_context(request: HttpRequest, listing_model, *, search_url: str) -> dict:
    """The vendor filter block's data: a window, the totals, and the search term.

    Windowed because the vendor list is the one unbounded facet - it will run to
    thousands - see ``vendors.services.vendor_facet``.

    The two catalogs' copies differed only in the model and the search URL, which are
    now the two parameters. The apparent hardware-specific nuance ("scoped to vendors
    with a published listing of *this* kind, so /systems/ does not advertise vendors
    that only made components") is not in the wrapper at all: it falls out of
    ``vendor_facet`` being given ``System`` rather than ``Component``.

    Both catalogs already render this through one shared template pair, so the
    read side was single-sourced before the context was.
    """
    from lumina.vendors.services import vendor_facet

    query = request.GET.get("vendor_q", "").strip()
    facet = vendor_facet(
        listing_model, selected=request.GET.getlist("vendor"), query=query,
    )
    return {
        "vendors": facet.vendors,
        "vendor_total": facet.matched,
        "vendor_pool_total": facet.pool,
        "vendor_query": query,
        "vendor_search_url": search_url,
    }
