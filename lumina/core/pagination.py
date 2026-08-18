"""One paginated, searchable section of a page.

A page can carry several of these at once - the dashboard has eight - so each is keyed, and the
key namespaces its own query parameters. That is what lets one section page forward without
resetting the others, which is the whole reason this is not a bare ``Paginator`` at each site.

**Searching is server-side, always.** The dashboard used to filter its tables in the browser over
whatever rows had been rendered, above lists silently cut off at two hundred. A search that
quietly only looks at what is already on screen does not say "no more results here"; it says "no
such thing", and a submitter hunting for their own run from last year was told it did not exist.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.core.paginator import Page, Paginator
from django.db.models import Q, QuerySet


@dataclass(frozen=True)
class Section:
    """A page of rows, plus everything a template needs to link to the others."""

    key: str
    page: Page
    query: str
    count: int
    # Every other parameter on the request, so paging one section keeps the rest of the page where
    # the reader put it: their filter on another table, their tab, their page number.
    keep: str
    hidden: list[tuple[str, str]]

    @property
    def param(self) -> str:
        return f"page_{self.key}" if self.key else "page"

    @property
    def search_param(self) -> str:
        return f"q_{self.key}" if self.key else "q"

    @property
    def base(self) -> str:
        """The query string to hang a page number off, ``?`` and separator included."""
        return f"?{self.keep}&" if self.keep else "?"

    @property
    def searchable(self) -> bool:
        """Whether a search box earns its place.

        A list that fits on one page is one the reader can already see, and a box over three rows
        is clutter on a page that has eight of them. Once a search is in force the box stays, or
        there is no way to change or clear it.
        """
        return self.count > self.page.paginator.per_page or bool(self.query)

    @property
    def paged(self) -> bool:
        """Whether the pager has anywhere to go."""
        return self.page.has_other_pages() or bool(self.query)

    def __bool__(self) -> bool:
        """True when there is anything to show, so a template can ask directly."""
        return bool(self.page.object_list)


def _build(request, paginator: Paginator, key: str, query: str) -> Section:
    page_param = f"page_{key}" if key else "page"
    search_param = f"q_{key}" if key else "q"
    keep = request.GET.copy()
    keep.pop(page_param, None)
    return Section(
        key=key,
        # ``get_page`` rather than ``page``: a hand-edited or stale page number is a wrong URL,
        # not a server error, and the last page is the honest answer to one past the end.
        page=paginator.get_page(request.GET.get(page_param)),
        query=query,
        count=paginator.count,
        keep=keep.urlencode(),
        # For the search form, which supplies its own term and must not resubmit this section's
        # page number: searching goes back to the first page of the new result, not to page 7 of
        # a list that no longer has seven pages.
        hidden=[(name, value) for name, value in request.GET.items()
                if name not in (page_param, search_param)],
    )


def _per_page(per_page: int | None) -> int:
    from lumina.core.models import DisplayDefaults

    return per_page or DisplayDefaults.per_page()


def paginate_rows(request, rows: list, key: str = "", *,
                  text=None, per_page: int | None = None) -> Section:
    """The same as ``paginate``, for a list that is not one queryset.

    The review archive merges seven models with different columns into one ordered list, which no
    database-level union can do without them sharing a shape. Searching in Python here is not the
    mistake the dashboard's browser-side filter was: the whole list is in hand, so the search
    covers all of it rather than whatever happened to have been rendered.

    ``text`` turns one row into the string a query matches against, so the search reads the same
    columns the table shows.
    """
    query = (request.GET.get(f"q_{key}" if key else "q") or "").strip()
    if query and text is not None:
        needle = query.casefold()
        rows = [row for row in rows if needle in (text(row) or "").casefold()]
    return _build(request, Paginator(rows, _per_page(per_page)), key, query)


def paginate(request, queryset: QuerySet, key: str = "", *,
             search: tuple[str, ...] = (), per_page: int | None = None) -> Section:
    """One section of a page: filtered by its own search box, cut into pages of its own.

    ``search`` names the model fields a query matches against, case-insensitively, any of them.
    Naming them per section rather than guessing keeps the search over columns a reader can see:
    matching a hidden internal field finds rows for reasons nobody can account for.

    ``per_page`` defaults to the site setting, so the number is one an admin can change rather
    than one buried at each call site.
    """
    query = (request.GET.get(f"q_{key}" if key else "q") or "").strip()
    if query and search:
        matching = Q()
        for field in search:
            matching |= Q(**{f"{field}__icontains": query})
        queryset = queryset.filter(matching)
    return _build(request, Paginator(queryset, _per_page(per_page)), key, query)
