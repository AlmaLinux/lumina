"""Every page the reviewer and submitter act on has to be well nested HTML.

Two reports came from broken nesting and nothing in the suite noticed either:

- "The 'Identity' block in the propose listing form is now completely blank" - a card rendered
  around fields that had all been dropped.
- "Why does it look awful now?" - merging two boxes on the review page left the replacement card
  self-contained while the section below it had been living inside the *old* card. Its wrapper
  went, the old card's closing tags stayed, and they closed the column and the row early, so
  everything after rendered outside the grid.

Every existing test of these templates asserts on strings in the body, which is blind to
structure: the copy was all present and correct in both cases. This parses the rendered output
and checks that every element closes in order, which is cheap and catches the whole class.
"""
from __future__ import annotations

from html.parser import HTMLParser

import pytest
from django.contrib.auth.models import Group, User
from django.urls import reverse

from lumina.hardware.models import System
from lumina.releases.models import AlmaLinuxRelease
from lumina.results import ingest
from lumina.results.tests import factories as f
from lumina.vendors.models import Vendor

pytestmark = pytest.mark.django_db

VOID = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
    "source", "track", "wbr",
}


class _Nesting(HTMLParser):
    """Shift-reduce over start and end tags. Void elements never go on the stack."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, tuple[int, int]]] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag not in VOID:
            self.stack.append((tag, self.getpos()))

    def handle_startendtag(self, tag, attrs):
        pass

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if not self.stack:
            self.errors.append(f"</{tag}> at {self.getpos()} closes nothing")
            return
        open_tag, open_pos = self.stack[-1]
        if open_tag != tag:
            self.errors.append(
                f"</{tag}> at line {self.getpos()[0]} but <{open_tag}> opened at line "
                f"{open_pos[0]} is still open"
            )
        self.stack.pop()


def _check(body: str) -> None:
    parser = _Nesting()
    parser.feed(body)
    assert not parser.errors, parser.errors[:5]
    assert not parser.stack, f"never closed: {[t for t, _ in parser.stack]}"


@pytest.fixture(autouse=True)
def releases():
    AlmaLinuxRelease.objects.get_or_create(major=9, defaults={"supported": True})


@pytest.fixture
def submitter():
    return User.objects.create_user("nest-sub", password="pw")


@pytest.fixture
def reviewer():
    user = User.objects.create_user("nest-rev", password="pw")
    group, _ = Group.objects.get_or_create(name="reviewer")
    user.groups.add(group)
    return user


def _run(submitter):
    return ingest.ingest_bundle(
        submitter=submitter, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"],
            results=[f.validate_result("validate.cpu.functional")],
        ))),
    )


def _listed(submitter):
    dell, _ = Vendor.objects.get_or_create(name="Dell Inc.", defaults={"published": True})
    System.objects.create(vendor=dell, name="PowerEdge R760")
    return _run(submitter)


# Both branches of every conditional block on these pages, because the bug was in one branch of
# one of them: a card that only renders for a matched run, wrapping a section that renders for
# every run.
def test_the_review_page_is_well_nested_for_a_new_machine(client, submitter, reviewer):
    run = _run(submitter)
    client.force_login(reviewer)

    _check(client.get(reverse("review:run_detail", args=[run.pk])).content.decode())


def test_the_review_page_is_well_nested_for_a_listed_machine(client, submitter, reviewer):
    """The reported case. `effect.creates` is False here, which is the branch that broke."""
    run = _listed(submitter)
    client.force_login(reviewer)

    _check(client.get(reverse("review:run_detail", args=[run.pk])).content.decode())


def test_the_propose_form_is_well_nested_when_creating(client, submitter):
    run = _run(submitter)
    client.force_login(submitter)

    _check(client.get(reverse("results:propose_listing", args=[run.uuid])).content.decode())


def test_the_propose_form_is_well_nested_when_locked(client, submitter):
    """The other reported case: the identity fields are locked here, so the card that used to
    render empty is the collapsed override instead."""
    run = _listed(submitter)
    client.force_login(submitter)

    _check(client.get(reverse("results:propose_listing", args=[run.uuid])).content.decode())


def test_the_public_run_page_is_well_nested(client, submitter):
    run = _run(submitter)
    client.force_login(submitter)

    _check(client.get(run.get_absolute_url()).content.decode())
