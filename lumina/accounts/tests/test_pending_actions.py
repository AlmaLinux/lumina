"""One block at the top of the dashboard saying what is waiting on the reader.

Asked for as a big combined call to action, because the page already carried the same information
per category and answering "is there anything for me to do" meant reading five tables and knowing
which statuses meant "yours".

Most of these tests are about what is deliberately *not* in it. A block that lists things the
reader cannot act on is a block they learn to scroll past, and it sits above everything else on a
page people open routinely, so that failure would cost the sections below their reader too.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import Group, User
from django.urls import reverse

from lumina.releases.models import AlmaLinuxRelease
from lumina.results import ingest, services
from lumina.results.models import TestRun
from lumina.results.tests import factories as f
from lumina.vendors.models import Vendor

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def alma_nine():
    AlmaLinuxRelease.objects.get_or_create(major=9, defaults={"supported": True})
    Vendor.objects.get_or_create(name="Dell Inc.", defaults={"published": True})


@pytest.fixture
def owner():
    return User.objects.create_user("pa-owner", password="pw")


@pytest.fixture
def reviewer():
    user = User.objects.create_user("pa-rev", password="pw")
    group, _ = Group.objects.get_or_create(name="reviewer")
    user.groups.add(group)
    return user


def _run(owner, status=TestRun.STATUS_DRAFT, **kw):
    run = ingest.ingest_bundle(
        submitter=owner, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"],
            results=[f.validate_result("validate.cpu.functional")], **kw,
        ))),
    )
    if status != run.status:
        run.status = status
        run.save(update_fields=["status"])
    return run


def _actions(client, user):
    client.force_login(user)
    return client.get(reverse("accounts:dashboard")).context["pending_actions"]


# --- what belongs in it ----------------------------------------------------------------


def test_an_unfinished_draft_is_an_action(client, owner):
    _run(owner)

    actions = _actions(client, owner)

    assert len(actions) == 1
    assert actions[0]["cta"] == "Finish submission"


def test_a_draft_says_what_it_is_missing(client, owner):
    """From what the code will actually check, not from the status label."""
    actions = _actions(client, owner) if _run(owner) else None

    assert "Still needed" in actions[0]["ask"]


def test_a_complete_draft_says_it_only_needs_releasing(client, owner):
    """A draft with nothing outstanding is real and common: a machine already in the catalog
    auto-links on release. Telling that submitter details are missing would be wrong."""
    run = _run(owner)
    run.listing_proposal = {"vendor_name": "Dell Inc.", "name": "PowerEdge R760",
                            "machine_kind": "prebuilt"}
    run.save(update_fields=["listing_proposal"])

    actions = _actions(client, owner)

    assert actions[0]["ask"] == "Complete. It needs releasing for review."


def test_a_bounced_run_carries_the_reviewers_words(client, owner, reviewer):
    run = _run(owner, TestRun.STATUS_PENDING)
    services.request_run_changes(run, by=reviewer, reason="Name the board properly.")

    actions = _actions(client, owner)

    assert actions[0]["cta"] == "Review and resubmit"
    assert actions[0]["note"] == "Name the board properly."


def test_several_drafts_of_one_machine_are_one_item(client, owner):
    """Nine drafts of one server is one answer. The run page they point at already offers to
    submit the whole batch."""
    _run(owner, version_id="9.6")
    _run(owner, version_id="10.1", run_id="aaaaaaaa-0000-0000-0000-000000000001")

    actions = _actions(client, owner)

    assert len(actions) == 1
    assert "2 runs" in actions[0]["title"]


def test_the_oldest_thing_comes_first(client, owner):
    """The item that has waited longest is the one most likely to have been forgotten.

    Two different machines, because two runs of the *same* machine collapse into one item and
    would test the grouping rather than the ordering.
    """
    # Both the system and the board, because ``sibling_runs`` matches on either one: a run whose
    # chassis differs but whose board is the same is still the same machine to it.
    inventory = f.make_report()["inventory"]
    inventory["summary"]["system"]["product"] = "PowerEdge R650"
    inventory["summary"]["baseboard"]["product"] = "0AAAAA"
    first = _run(owner, inventory=inventory)
    _run(owner, run_id="aaaaaaaa-0000-0000-0000-000000000002")

    actions = _actions(client, owner)

    assert len(actions) == 2, "the premise: two machines, two items"
    assert actions[0]["since"] == first.received_at


# --- what must stay out of it ------------------------------------------------------------


def test_a_run_awaiting_review_is_not_an_action(client, owner):
    """It is information. The ball is with a reviewer, and listing it here would make the block
    something to scroll past."""
    _run(owner, TestRun.STATUS_PENDING)

    assert _actions(client, owner) == []


def test_a_quarantined_run_is_not_an_action(client, owner):
    """It looks alarming and is somebody else's move: only a reviewer can release one."""
    _run(owner, TestRun.STATUS_QUARANTINED)

    assert _actions(client, owner) == []


@pytest.mark.parametrize("status", [TestRun.STATUS_APPROVED, TestRun.STATUS_REJECTED])
def test_finished_work_is_not_an_action(client, owner, status):
    _run(owner, status)

    assert _actions(client, owner) == []


def test_an_archived_draft_is_not_an_action(client, owner):
    """The whole point of archiving is saying "I am not taking this further". Without this the
    block drags back to the top of the page exactly what the reader just put away."""
    run = _run(owner)
    services.archive_run(run, by=owner)

    assert _actions(client, owner) == []


def test_somebody_elses_work_is_not_an_action(client, owner):
    stranger = User.objects.create_user("pa-stranger", password="pw")
    _run(stranger)

    assert _actions(client, owner) == []


# --- the block itself ---------------------------------------------------------------------


def test_nothing_renders_when_there_is_nothing_to_do(client, owner):
    """No card and no "all caught up". A region that is always there and usually says nothing is
    a region people learn to skip."""
    client.force_login(owner)

    body = client.get(reverse("accounts:dashboard")).content.decode()

    assert "Waiting on you" not in body


def test_it_renders_above_the_quick_actions(client, owner):
    """Somebody opens this page to find out whether anything needs them. That answer should not
    be below three cards offering to start something new."""
    _run(owner)
    client.force_login(owner)

    body = client.get(reverse("accounts:dashboard")).content.decode()

    # The card's own heading, not the sidebar link of the same name, which is in every layout
    # above everything in the content block.
    assert body.index("Waiting on you") < body.index(
        '<h2 class="h5 mb-0">Submit hardware</h2>'
    )


def test_every_item_has_somewhere_to_go(client, owner, reviewer):
    """An item with no button is not an action. This is why vendor claims are excluded: a claim
    sent back for more evidence really is waiting on the claimant, and there is no route that
    lets them answer."""
    _run(owner)

    for item in _actions(client, owner):
        assert item["url"] and item["cta"]
