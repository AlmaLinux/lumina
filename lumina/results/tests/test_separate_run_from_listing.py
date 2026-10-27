"""A reviewer separating a run from the System it was matched to.

Reported twice over: a reviewer could not make a pending run a new system when it had been
submitted against an existing one, and could not split an approved run off afterwards.

Identity matching is a heuristic over firmware strings, and two different machines can report the
same vendor and model - a rebadge, a barebones chassis several integrators sell. The run model
already had the answer: ``identity_disputed`` makes a run new hardware everywhere it is consulted.
But only the submitter could set it, and clearing the reviewer's system picker did nothing, because
approval matched the same listing again.

Before approval, separating is that flag plus a name. After approval it also moves the evidence:
the old System loses this run's attestation and is re-derived, and a new listing is certified from
the run the way approval would have.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import Group, User
from django.urls import reverse

from lumina.audit.models import AuditLogEntry
from lumina.hardware.models import CommunityAttestation, ComponentKind, ListingVersion, System
from lumina.results import ingest, services
from lumina.results.models import ReportedIdentityAlias, TestRun
from lumina.results.tests import factories as f
from lumina.results.tests.helpers import release
from lumina.vendors.models import Vendor

pytestmark = pytest.mark.django_db

# The default fixture reports Dell Inc. / PowerEdge R760. Two machines reporting exactly this is
# the case: the run matches the listing because the strings agree, and is not that machine.
ACME = {"vendor_name": "Acme Integrators", "name": "Acme Box 1", "model_number": ""}


@pytest.fixture
def reviewer(client):
    user = User.objects.create_user("sep-rev")
    user.groups.add(Group.objects.get_or_create(name="reviewer")[0])
    client.force_login(user)
    return user


@pytest.fixture
def poweredge():
    dell, _ = Vendor.objects.get_or_create(name="Dell Inc.", defaults={"published": True})
    return System.objects.create(vendor=dell, name="PowerEdge R760", published=True)


def _run(username="sep-sub") -> TestRun:
    submitter = User.objects.get_or_create(username=username)[0]
    run = ingest.ingest_bundle(
        submitter=submitter, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"],
            results=[f.validate_result("validate.cpu.functional")],
        ))),
    )
    return release(TestRun.objects.get(pk=run.pk))


def _approved(poweredge, reviewer, username="sep-sub") -> TestRun:
    run = _run(username)
    assert run.listing_system == poweredge, "the premise: auto-linked by its strings"
    services.approve_run(run, by=reviewer)
    run.refresh_from_db()
    assert CommunityAttestation.objects.filter(test_run=run, listing_system=poweredge).exists()
    return run


# --- pending -------------------------------------------------------------------------


def test_a_pending_run_stops_being_evidence_for_the_match(poweredge, reviewer):
    """The first report: a reviewer could not say "this is a new system"."""
    run = _run()

    services.separate_run_from_listing(run, by=reviewer, identity=ACME)

    run.refresh_from_db()
    assert run.listing_system is None
    assert run.identity_disputed is True
    assert run.listing_proposal["name"] == "Acme Box 1"


def test_approving_it_then_creates_its_own_listing(poweredge, reviewer):
    """The flag is what makes this stick. Clearing the picker alone did not: approval matched
    the same listing again by its strings."""
    run = _run()
    services.separate_run_from_listing(run, by=reviewer, identity=ACME)
    run.refresh_from_db()

    services.approve_run(run, by=reviewer)

    run.refresh_from_db()
    assert run.listing_system.name == "Acme Box 1"
    assert not CommunityAttestation.objects.filter(listing_system=poweredge).exists()


# --- approved ------------------------------------------------------------------------


def test_an_approved_run_is_split_off(poweredge, reviewer):
    """The second report: no easy way to split an approved run off afterwards."""
    run = _approved(poweredge, reviewer)

    services.separate_run_from_listing(run, by=reviewer, identity=ACME)

    run.refresh_from_db()
    assert run.listing_system.name == "Acme Box 1"
    assert run.listing_system.published
    assert CommunityAttestation.objects.filter(
        test_run=run, listing_system=run.listing_system).exists()
    assert not CommunityAttestation.objects.filter(
        test_run=run, listing_system=poweredge).exists()


def test_the_old_listing_is_unpublished_when_this_was_its_only_evidence(poweredge, reviewer):
    """An unattested listing has nothing public behind it. Publishing is what attesting does,
    so the reverse falls to whoever takes the last attestation away."""
    run = _approved(poweredge, reviewer)

    services.separate_run_from_listing(run, by=reviewer, identity=ACME)

    poweredge.refresh_from_db()
    assert poweredge.attestation_count == 0
    assert poweredge.published is False


def test_the_old_listing_keeps_everybody_elses_evidence(poweredge, reviewer):
    """One run being misidentified is not grounds to retract another person's evidence about
    the machine it was wrongly matched to."""
    _approved(poweredge, reviewer, username="genuine-owner")
    run = _approved(poweredge, reviewer, username="sep-sub")

    services.separate_run_from_listing(run, by=reviewer, identity=ACME)

    poweredge.refresh_from_db()
    assert poweredge.attestation_count == 1
    assert poweredge.published is True


def test_a_release_left_with_no_evidence_is_not_still_claimed_as_proven(poweredge, reviewer):
    """Reverted to a declaration rather than deleted: the row cannot say whether it was a
    vendor's declaration this run upgraded, so deleting could destroy a real claim - but
    leaving it marked run-proven would claim evidence that is gone."""
    run = _approved(poweredge, reviewer)

    services.separate_run_from_listing(run, by=reviewer, identity=ACME)

    version = ListingVersion.objects.get(listing_system=poweredge)
    assert version.source == ListingVersion.SOURCE_DECLARED


def test_the_parts_stay_certified(poweredge, reviewer):
    """The machine was misidentified, not the hardware. The CPU this run proved is still
    proven, so separating must not reach the components' own attestations."""
    run = _approved(poweredge, reviewer)
    cpu = run.listing_components.get(kind=ComponentKind.cpu.value)
    before = cpu.attestation_count

    services.separate_run_from_listing(run, by=reviewer, identity=ACME)

    cpu.refresh_from_db()
    assert cpu.attestation_count == before


def test_a_part_only_this_run_tied_leaves_the_old_machine(poweredge, reviewer):
    """Otherwise the old listing keeps advertising a CPU nobody ever ran on it."""
    run = _approved(poweredge, reviewer)
    cpu = run.listing_components.get(kind=ComponentKind.cpu.value)

    services.separate_run_from_listing(run, by=reviewer, identity=ACME)

    assert not poweredge.cpus.filter(pk=cpu.pk).exists()
    run.refresh_from_db()
    assert run.listing_system.cpus.filter(pk=cpu.pk).exists(), "and it moves with the run"


def test_a_part_another_run_still_ties_stays(poweredge, reviewer):
    """The old System's parts are the union of every run's evidence. Removing one another run
    still vouches for would erase that run's statement along with this one's."""
    _approved(poweredge, reviewer, username="genuine-owner")
    run = _approved(poweredge, reviewer, username="sep-sub")
    cpu = run.listing_components.get(kind=ComponentKind.cpu.value)

    services.separate_run_from_listing(run, by=reviewer, identity=ACME)

    assert poweredge.cpus.filter(pk=cpu.pk).exists()


# --- the match does not come back -----------------------------------------------------


def test_the_next_run_of_the_original_machine_still_lands_on_it(poweredge, reviewer):
    """The trap in moving evidence by firmware strings. Creating the new listing records an
    alias for this run's strings - and aliases are consulted *before* name matching. So every
    later run of the genuine PowerEdge, reporting exactly the same strings, would be filed under
    the integrator's box instead.

    Separating is the statement that these strings do not identify one machine. An alias is the
    statement that they do."""
    run = _approved(poweredge, reviewer)
    services.separate_run_from_listing(run, by=reviewer, identity=ACME)

    later = _run(username="another-poweredge-owner")

    assert later.listing_system == poweredge


def test_no_alias_claims_the_shared_strings_for_the_new_listing(poweredge, reviewer):
    """The same thing stated as data, so a failure says which part went wrong."""
    run = _approved(poweredge, reviewer)

    services.separate_run_from_listing(run, by=reviewer, identity=ACME)

    run.refresh_from_db()
    assert not ReportedIdentityAlias.objects.filter(listing_system=run.listing_system).exists()


# --- refusals -------------------------------------------------------------------------


def test_the_same_name_is_refused(poweredge, reviewer):
    """It matched because the strings agree, so left alone it would be created under the same
    name - two listings nobody reading the catalog could tell apart."""
    run = _run()

    with pytest.raises(services.ReviewError, match="tells it apart"):
        services.separate_run_from_listing(
            run, by=reviewer, identity={"vendor_name": "Dell Inc.", "name": "powerEdge r760"})

    run.refresh_from_db()
    assert run.listing_system == poweredge, "and nothing changed"


def test_a_custom_build_is_refused_with_the_way_out(reviewer):
    """One System per motherboard is the design for custom builds, so a second System for the
    same board would break it. The message says what to do instead."""
    run = ingest.ingest_bundle(
        submitter=User.objects.create_user("custom-sub"), source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"], inventory=f.custom_build_inventory(),
            results=[f.validate_result("validate.cpu.functional")],
        ))),
    )
    run = release(TestRun.objects.get(pk=run.pk))
    services.approve_run(run, by=reviewer)
    run.refresh_from_db()

    assert services.can_separate(run) is False
    with pytest.raises(services.ReviewError, match="vendor-built"):
        services.separate_run_from_listing(run, by=reviewer, identity=ACME)


def test_a_run_matched_to_nothing_is_refused(reviewer):
    run = _run()
    assert run.listing_system is None

    assert services.can_separate(run) is False
    with pytest.raises(services.ReviewError):
        services.separate_run_from_listing(run, by=reviewer, identity=ACME)


# --- the page -------------------------------------------------------------------------


def test_the_control_is_offered_where_the_match_is_named(client, poweredge, reviewer):
    run = _run()

    body = client.get(reverse("review:run_detail", args=[run.pk])).content.decode()

    assert "Give it a listing of its own" in body
    assert reverse("review:run_separate_listing", args=[run.pk]) in body


def test_it_is_not_offered_where_it_would_be_refused(client, reviewer):
    run = _run()

    body = client.get(reverse("review:run_detail", args=[run.pk])).content.decode()

    assert "Give it a listing of its own" not in body


def test_the_endpoint_does_it(client, poweredge, reviewer):
    run = _approved(poweredge, reviewer)

    client.post(reverse("review:run_separate_listing", args=[run.pk]), ACME)

    run.refresh_from_db()
    assert run.listing_system.name == "Acme Box 1"
    entry = AuditLogEntry.objects.get(action="test_run.separated")
    assert entry.before["listing"] == str(poweredge)


def test_a_refusal_reaches_the_reviewer_rather_than_a_500(client, poweredge, reviewer):
    run = _run()

    response = client.post(reverse("review:run_separate_listing", args=[run.pk]),
                           {"vendor_name": "Dell Inc.", "name": "PowerEdge R760"}, follow=True)

    assert response.status_code == 200
    assert "tells it apart" in response.content.decode()


def test_a_submitter_dispute_does_not_hijack_the_strings_either(poweredge, reviewer):
    """The same hole, reachable before this change. A submitter could always dispute a match,
    and approving their run recorded the alias that sent every later run of the original
    machine to theirs. Fixed where the alias is written, so both paths are covered at once."""
    run = _run()
    run.identity_disputed = True
    run.listing_system = None
    run.listing_proposal = {**ACME, "machine_kind": "prebuilt"}
    run.save(update_fields=["identity_disputed", "listing_system", "listing_proposal"])
    services.approve_run(run, by=reviewer)

    later = _run(username="another-poweredge-owner")

    assert later.listing_system == poweredge
