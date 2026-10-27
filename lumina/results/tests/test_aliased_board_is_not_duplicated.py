"""A custom build whose board a reviewer already identified gets that board, not a second one.

Found while fixing approval for reopened runs. Firmware often names a motherboard badly - "OEM BOARD
X", "Default string" - and a reviewer resolves it once: a ``ReportedIdentityAlias`` saying these
strings mean the ASRock B650M PG Riptide. Ingest honors it and links the real board. Then approval
tied the parts, and ``ensure_component_ties`` looked the board up by the raw firmware string, found
nothing called "OEM BOARD X", and created one. Every later run of the machine did the same lookup,
so the catalog grew a junk board beside the real one and the machine's evidence was split across
both.

Every place that resolves a board now asks ``known_board`` first: the approval tie, the review
page's preview of it - which has to say what approval will do - the custom build's System, and the
machine-listing lookup. A human correction for this run still wins over the alias, because it was
made about this run.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import Group, User

from lumina.hardware.models import Component, ComponentKind, System
from lumina.results import ingest, services
from lumina.results.component_match import find_or_create_component
from lumina.results.models import ReportedIdentityAlias, TestRun
from lumina.results.tests import factories as f
from lumina.results.tests.helpers import release
from lumina.vendors.models import Vendor

pytestmark = pytest.mark.django_db

UNHELPFUL = "OEM BOARD X"


@pytest.fixture
def reviewer():
    user = User.objects.create_user("board-rev")
    user.groups.add(Group.objects.get_or_create(name="reviewer")[0])
    return user


@pytest.fixture
def riptide():
    asrock, _ = Vendor.objects.get_or_create(name="ASRock")
    board, _ = find_or_create_component(asrock, "B650M PG Riptide", ComponentKind.motherboard)
    return board


def _alias(board):
    return ReportedIdentityAlias.objects.create(
        reported_vendor="ASRock", reported_product=UNHELPFUL, listing_component=board)


def _run(username="board-sub") -> TestRun:
    inventory = f.custom_build_inventory()
    inventory["summary"]["system"]["product"] = UNHELPFUL
    inventory["summary"]["baseboard"]["product"] = UNHELPFUL
    run = ingest.ingest_bundle(
        submitter=User.objects.get_or_create(username=username)[0], source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"], inventory=inventory,
            results=[f.validate_result("validate.cpu.functional")],
        ))),
    )
    return release(TestRun.objects.get(pk=run.pk))


def _boards():
    return list(Component.objects.filter(
        kind=ComponentKind.motherboard.value).values_list("name", flat=True))


def test_approval_ties_the_board_the_alias_names(riptide, reviewer):
    """The report: a second board appeared under the firmware's name."""
    _alias(riptide)
    run = _run()

    services.approve_run(run, by=reviewer)

    assert _boards() == ["B650M PG Riptide"]
    assert run.listing_components.filter(pk=riptide.pk).exists()


def test_every_later_run_lands_on_the_same_board(riptide, reviewer):
    """Each run made its own junk board before, splitting the machine's evidence."""
    _alias(riptide)
    for name in ("first", "second", "third"):
        services.approve_run(_run(name), by=reviewer)

    assert _boards() == ["B650M PG Riptide"]


def test_the_machine_listing_is_the_aliased_boards_system(riptide, reviewer):
    """A custom build's System is its board promoted, so the wrong board meant the wrong System -
    "OEM BOARD X" listed under Systems for a machine that has a real name."""
    _alias(riptide)
    run = _run()

    services.approve_run(run, by=reviewer)

    run.refresh_from_db()
    assert run.listing_system.name == "B650M PG Riptide"
    assert not System.objects.filter(name=UNHELPFUL).exists()


def test_an_alias_written_after_ingest_still_applies(riptide, reviewer):
    """Ingest only links what existed when the run arrived. An alias a reviewer writes while the
    run waits in the queue has to reach its approval too."""
    run = _run()
    assert not run.listing_components.exists(), "the premise: nothing linked at ingest"
    _alias(riptide)

    services.approve_run(run, by=reviewer)

    assert _boards() == ["B650M PG Riptide"]
    run.refresh_from_db()
    assert run.listing_system.name == "B650M PG Riptide"


def test_the_preview_says_what_approval_will_do(riptide, reviewer):
    """The review page's component list is the same resolution, reported rather than applied. It
    said a board called "OEM BOARD X" would be created; approval now attaches the real one, and a
    preview that disagrees with approval is worse than none."""
    _alias(riptide)
    run = _run()

    board = next(row for row in services.preview_component_ties(run)
                 if row["kind"] == ComponentKind.motherboard.value)

    assert board["component"] == riptide


def test_a_reviewers_correction_for_this_run_still_wins(riptide, reviewer):
    """The alias is what these strings usually mean. A reviewer who corrected this run's board
    has said what it means here, and that was made about this machine."""
    _alias(riptide)
    run = _run()
    key = next(t["key"] for t in services.component_tie_targets(run)
               if t["kind"] == ComponentKind.motherboard)
    run.component_overrides = {key: {"model": "X870E Taichi"}}
    run.save(update_fields=["component_overrides"])

    services.approve_run(run, by=reviewer)

    assert "X870E Taichi" in _boards()


def test_without_an_alias_nothing_changes(reviewer):
    """The ordinary custom build, resolved by name exactly as before."""
    run = ingest.ingest_bundle(
        submitter=User.objects.create_user("plain-builder"), source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"], inventory=f.custom_build_inventory(),
            results=[f.validate_result("validate.cpu.functional")],
        ))),
    )
    services.approve_run(release(TestRun.objects.get(pk=run.pk)), by=reviewer)

    assert _boards() == ["B650M PG Riptide"]


def test_excluding_an_aliased_board_unties_it(riptide, reviewer):
    """Untying resolves a part the way the tie was made. Looked up by the firmware's name, an
    alias-linked board was never found, so excluding it detached nothing."""
    _alias(riptide)
    run = _run()
    services.approve_run(run, by=reviewer)
    run.refresh_from_db()
    assert run.listing_components.filter(pk=riptide.pk).exists()

    run.excluded_component_ties = [
        t["key"] for t in services.component_tie_targets(run)
        if t["kind"] == ComponentKind.motherboard]
    run.save(update_fields=["excluded_component_ties"])
    services.ensure_component_ties(run)

    assert not run.listing_components.filter(pk=riptide.pk).exists()


def test_a_board_name_somebody_typed_is_theirs_to_resolve(riptide, reviewer):
    """Only the name the machine reported defers to the alias. Somebody who named this run's board
    something else has said what it is here, and the create path resolves what they typed."""
    run = _run()
    _alias(riptide)
    run.listing_proposal = {**(run.listing_proposal or {}),
                            "vendor_name": "ASRock", "name": "X870E Taichi"}
    run.save(update_fields=["listing_proposal"])

    services.approve_run(run, by=reviewer)

    run.refresh_from_db()
    assert run.listing_system.name == "X870E Taichi"


def test_an_alias_to_some_other_kind_of_part_is_not_the_board(reviewer):
    """An alias is keyed on reported strings, and a custom build's system strings often mirror its
    board's. A mapping from those strings to a CPU is not a statement about the motherboard, and
    tying it on as one would file a processor as the machine's board."""
    amd, _ = Vendor.objects.get_or_create(name="AMD")
    cpu, _ = find_or_create_component(amd, "Ryzen 9 7950X", ComponentKind.cpu)
    ReportedIdentityAlias.objects.create(
        reported_vendor="ASRock", reported_product=UNHELPFUL, listing_component=cpu)
    run = _run()

    assert services.find_matching_board("ASRock", UNHELPFUL) is None
    assert services.known_board(run) is None


def test_a_board_a_reviewer_linked_by_hand_is_the_board(riptide, reviewer):
    """No alias at all: a reviewer attached the real board to this run in the assign form. That is
    a statement about this run, and approval ties what they attached rather than minting a board
    from the firmware string."""
    run = _run()
    services.assign_listing(run, system=None, components=[riptide], by=reviewer)

    services.approve_run(TestRun.objects.get(pk=run.pk), by=reviewer)

    assert _boards() == ["B650M PG Riptide"]
