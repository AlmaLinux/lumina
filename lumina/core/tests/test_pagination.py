"""Long lists page, and their search boxes ask the server.

The dashboard cut every list off at two hundred rows with nothing saying so, and its filter boxes
matched against whatever rows had been rendered. A search that quietly only looks at what is
already on screen does not say "nothing more on this page"; it says "no such thing".
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import RequestFactory

from lumina.core.models import DisplayDefaults
from lumina.core.pagination import paginate, paginate_rows

pytestmark = pytest.mark.django_db


def _request(**params):
    return RequestFactory().get("/", params)


def _users(count: int) -> None:
    User.objects.bulk_create([User(username=f"u{n:03d}") for n in range(count)])


def _names(section) -> list[str]:
    return [u.username for u in section.page]


# --- the page size is a setting -----------------------------------------------------


def test_the_default_page_size_is_twenty():
    _users(25)

    assert len(_names(paginate(_request(), User.objects.order_by("username")))) == 20


def test_an_admin_can_change_it_without_a_deploy():
    """The whole reason it is a row: a constant in code meant the number nobody had thought hard
    about was the number everybody lived with."""
    _users(25)
    DisplayDefaults.objects.update_or_create(pk=1, defaults={"rows_per_page": 5})

    assert len(_names(paginate(_request(), User.objects.order_by("username")))) == 5


def test_a_page_size_of_zero_does_not_take_the_page_down():
    """``Paginator`` raises on a per-page of nothing, and that would be every page on the site."""
    _users(3)
    DisplayDefaults.objects.update_or_create(pk=1, defaults={"rows_per_page": 0})

    assert len(_names(paginate(_request(), User.objects.order_by("username")))) == 1


def test_a_caller_may_still_ask_for_its_own_size():
    _users(10)

    assert len(_names(paginate(_request(), User.objects.order_by("username"), per_page=3))) == 3


# --- paging ------------------------------------------------------------------------


def test_the_second_page_holds_the_next_rows():
    _users(25)

    section = paginate(_request(page="2"), User.objects.order_by("username"))

    assert _names(section)[0] == "u020"
    assert section.count == 25, "the count is the whole result, not the page"


def test_a_page_past_the_end_lands_on_the_last_one():
    """A stale link or a hand-edited URL is a wrong address, not a server error."""
    _users(25)

    assert paginate(_request(page="99"), User.objects.order_by("username")).page.number == 2


def test_a_page_that_is_not_a_number_is_the_first_one():
    _users(25)

    assert paginate(_request(page="soon"), User.objects.order_by("username")).page.number == 1


# --- several sections on one page ---------------------------------------------------


def test_each_section_pages_on_its_own():
    """Eight lists on the dashboard, and paging one must not reset the others."""
    _users(25)

    left = paginate(_request(page_left="2"), User.objects.order_by("username"), "left")
    right = paginate(_request(page_left="2"), User.objects.order_by("username"), "right")

    assert left.page.number == 2
    assert right.page.number == 1


def test_a_page_link_carries_every_other_section_s_state():
    """Otherwise paging one table throws away the reader's filter on another."""
    section = paginate(
        _request(page_left="2", q_right="dell", page_right="3"),
        User.objects.all(), "left")

    assert "q_right=dell" in section.base and "page_right=3" in section.base
    assert "page_left" not in section.base, "or the link would carry two page numbers"


def test_the_search_form_does_not_resubmit_its_own_page_number():
    """A new search belongs on the first page of the new result, not on page 7 of a list that may
    no longer have seven pages."""
    section = paginate(_request(page_left="7", q_left="old", kind="gpu"),
                       User.objects.all(), "left")
    carried = dict(section.hidden)

    assert "page_left" not in carried and "q_left" not in carried
    assert carried["kind"] == "gpu"


# --- searching ---------------------------------------------------------------------


def test_a_search_matches_across_the_whole_list_not_the_page():
    """The row this is about is on page two, and the old browser-side filter could not see it."""
    _users(25)

    section = paginate(_request(q="u021"), User.objects.order_by("username"),
                       search=("username",))

    assert _names(section) == ["u021"]


def test_a_search_matches_any_of_the_named_fields():
    User.objects.create_user("alice", first_name="Zoe")
    User.objects.create_user("bob", first_name="Ann")

    section = paginate(_request(q="zoe"), User.objects.order_by("username"),
                       search=("username", "first_name"))

    assert _names(section) == ["alice"]


def test_a_search_ignores_case():
    User.objects.create_user("Alice")

    assert _names(paginate(_request(q="alice"), User.objects.all(), search=("username",))) == [
        "Alice"]


def test_a_section_with_no_search_fields_ignores_a_query():
    """Rather than filtering on a column nobody chose, or crashing."""
    _users(3)

    assert len(_names(paginate(_request(q="nothing"), User.objects.all()))) == 3


def test_the_query_comes_back_for_the_box_to_show():
    assert paginate(_request(q_left=" dell "), User.objects.all(), "left").query == "dell"


# --- a list that is not one queryset ------------------------------------------------


def test_a_merged_list_pages_too():
    """The review archive merges seven models with different columns, which no database-level
    union can do without them sharing a shape."""
    rows = [{"subject": f"row {n}"} for n in range(25)]

    section = paginate_rows(_request(page="2"), rows)

    assert len(list(section.page)) == 5
    assert section.count == 25


def test_a_merged_list_searches_over_all_of_it():
    rows = [{"subject": f"row {n}"} for n in range(25)]

    section = paginate_rows(_request(q="row 21"), rows, text=lambda r: r["subject"])

    assert list(section.page) == [{"subject": "row 21"}]


def test_a_merged_list_with_no_text_reader_ignores_a_query():
    rows = [{"subject": "one"}]

    assert len(list(paginate_rows(_request(q="x"), rows).page)) == 1


def test_a_section_is_falsey_when_this_page_is_empty():
    """So a template can ask about the rows it is about to render rather than about the total."""
    assert not paginate(_request(), User.objects.none())
    _users(1)
    assert paginate(_request(), User.objects.all())
