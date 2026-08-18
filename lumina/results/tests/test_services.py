"""Review actions, embargo publication, and attestation coupling."""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth.models import Group, User
from django.utils import timezone

from lumina.core.certification import ValidationLevel
from lumina.hardware.models import CommunityAttestation, System
from lumina.results import ingest, services
from lumina.results.models import TestRun
from lumina.results.tests import factories as f
from lumina.results.tests.helpers import release
from lumina.vendors.models import Vendor, VendorMembership

pytestmark = pytest.mark.django_db



@pytest.fixture
def submitter():
    return User.objects.create_user("submitter")


@pytest.fixture
def reviewer():
    user = User.objects.create_user("reviewer")
    group, _ = Group.objects.get_or_create(name="reviewer")
    user.groups.add(group)
    return user


@pytest.fixture
def vendor():
    return Vendor.objects.create(name="ACME", verified=True, published=True)


@pytest.fixture
def system(vendor):
    return System.objects.create(name="R760", vendor=vendor, owner_vendor=vendor)


def _make_run(submitter, *, results=None, run_types=None, pre_release=False,
              publish_after=None, system=None) -> TestRun:
    report = f.make_report(
        run_types=run_types or ["validate"],
        results=results if results is not None
        else [f.validate_result("validate.cpu.functional")],
        pre_release=pre_release,
        publish_after=publish_after,
    )
    bundle = f.as_upload(f.build_bundle(report))
    run = ingest.ingest_bundle(submitter=submitter, bundle_file=bundle, source="api")
    if system is not None:
        run.listing_system = system
        run.save(update_fields=["listing_system"])
    return release(run)


# --- approve / reject / request changes --------------------------------------


def test_approve_publishes_immediately_without_embargo(submitter, reviewer):
    run = _make_run(submitter)
    services.approve_run(release(run), by=reviewer)
    run.refresh_from_db()
    assert run.status == TestRun.STATUS_APPROVED
    assert run.published_at is not None
    assert run.is_public
    assert run in TestRun.objects.public()


def test_approve_with_future_embargo_stays_unpublished(submitter, reviewer):
    future = (timezone.localdate() + timedelta(days=30)).isoformat()
    run = _make_run(submitter, pre_release=True, publish_after=future)
    services.approve_run(release(run), by=reviewer)
    run.refresh_from_db()
    assert run.status == TestRun.STATUS_APPROVED
    assert run.published_at is None
    assert run.is_embargoed
    assert run not in TestRun.objects.public()


def test_approve_with_past_embargo_date_publishes(submitter, reviewer):
    run = _make_run(submitter, pre_release=True, publish_after="2020-01-01")
    services.approve_run(release(run), by=reviewer)
    run.refresh_from_db()
    assert run.published_at is not None


def test_reject_and_double_action_guard(submitter, reviewer):
    run = _make_run(submitter)
    services.reject_run(run, by=reviewer, reason="implausible numbers")
    run.refresh_from_db()
    assert run.status == TestRun.STATUS_REJECTED
    assert run.reviewer_notes == "implausible numbers"
    with pytest.raises(services.ReviewError):
        services.approve_run(release(run), by=reviewer)


def test_request_changes_then_approve(submitter, reviewer):
    run = _make_run(submitter)
    services.request_run_changes(run, by=reviewer, reason="need dmesg artifact")
    run.refresh_from_db()
    assert run.status == TestRun.STATUS_NEEDS_CHANGES
    services.approve_run(release(run), by=reviewer)
    run.refresh_from_db()
    assert run.status == TestRun.STATUS_APPROVED


# --- embargo auto-publish -----------------------------------------------------


def test_publish_due_runs_releases_only_due_embargoes(submitter, reviewer):
    soon = (timezone.localdate() + timedelta(days=1)).isoformat()
    far = (timezone.localdate() + timedelta(days=90)).isoformat()
    due = _make_run(submitter, pre_release=True, publish_after=soon)
    not_due = _make_run(submitter, pre_release=True, publish_after=far)
    services.approve_run(release(due), by=reviewer)
    services.approve_run(release(not_due), by=reviewer)

    released = services.publish_due_runs(
        today=timezone.localdate() + timedelta(days=1)
    )
    assert [r.pk for r in released] == [due.pk]
    due.refresh_from_db()
    not_due.refresh_from_db()
    assert due.published_at is not None
    assert not_due.published_at is None
    # pending (unapproved) runs are never auto-published
    pending = _make_run(submitter, pre_release=True, publish_after="2020-01-01")
    assert pending.pk not in [
        r.pk for r in services.publish_due_runs(today=timezone.localdate())
    ]


# --- attestation coupling ------------------------------------------------------


def _passing_validate_run(submitter, system, level="", on_behalf_of=None) -> TestRun:
    run = _make_run(submitter, system=system)
    run.claimed_validation_level = level
    run.on_behalf_of = on_behalf_of
    run.save(update_fields=["claimed_validation_level", "on_behalf_of"])
    return run


def test_approving_linked_passing_run_creates_attestation(submitter, reviewer, system):
    run = _passing_validate_run(submitter, system)
    services.approve_run(release(run), by=reviewer)
    system.refresh_from_db()
    assert system.attestation_count == 1
    assert system.published is True
    attestation = CommunityAttestation.objects.get(test_run=run, listing_system=system)
    assert attestation.source == "test_run"
    assert attestation.attested_by == submitter
    # the run's CPU is tied to the catalog automatically alongside the system,
    # at family granularity because a seeded family matches it
    cpu = run.listing_components.get(kind="cpu")
    assert cpu.name == "Intel Xeon Scalable 4th Generation"
    assert cpu.is_family
    assert cpu in system.cpus.all()
    assert CommunityAttestation.objects.filter(
        test_run=run, listing_component=cpu
    ).exists()


def test_repeat_runs_from_same_submitter_do_not_inflate_count(
    submitter, reviewer, system
):
    first = _passing_validate_run(submitter, system)
    services.approve_run(release(first), by=reviewer)
    second = _passing_validate_run(submitter, system)
    services.approve_run(release(second), by=reviewer)
    system.refresh_from_db()
    assert system.attestation_count == 1
    assert CommunityAttestation.objects.filter(listing_system=system).count() == 1


def test_different_submitters_each_count(reviewer, system):
    for name in ("alice", "bob"):
        user = User.objects.create_user(name)
        run = _passing_validate_run(user, system)
        services.approve_run(release(run), by=reviewer)
    system.refresh_from_db()
    assert system.attestation_count == 2


def test_failing_run_never_attests(submitter, reviewer, system):
    report = f.make_report(
        run_types=["validate"],
        results=[f.validate_result("validate.cpu.functional", status="fail")],
    )
    run = ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(f.build_bundle(report)),
        source="api",
    )
    run.listing_system = system
    run.save()
    services.approve_run(release(run), by=reviewer)
    system.refresh_from_db()
    assert system.attestation_count == 0
    assert not CommunityAttestation.objects.filter(test_run=run).exists()


def test_benchmark_runs_never_touch_trust_machinery(submitter, reviewer, system):
    report = f.make_report(run_types=["benchmark"], results=[f.benchmark_result()])
    run = ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(f.build_bundle(report)),
        source="api",
    )
    run.listing_system = system
    run.save()
    services.approve_run(release(run), by=reviewer)
    system.refresh_from_db()
    assert system.attestation_count == 0


def test_claimed_level_is_capped_by_submitter_entitlement(submitter, reviewer, system):
    """A plain user claiming 'almalinux' gets capped to 'community' - an automated
    run can never grant more trust than its submitter has."""
    run = _passing_validate_run(submitter, system, level=ValidationLevel.ALMALINUX)
    services.approve_run(release(run), by=reviewer)
    system.refresh_from_db()
    assert system.validation_level == ValidationLevel.COMMUNITY


def test_vendor_member_can_claim_vendor_level(reviewer, vendor, system):
    member = User.objects.create_user("vendor-eng")
    VendorMembership.objects.create(
        user=member, vendor=vendor, role=VendorMembership.ROLE_SUBMITTER
    )
    # Naming the vendor is what makes it the vendor's validation; membership
    # alone no longer implies it.
    run = _passing_validate_run(member, system, level=ValidationLevel.VENDOR,
                                on_behalf_of=vendor)
    services.approve_run(release(run), by=reviewer)
    system.refresh_from_db()
    assert system.validation_level == ValidationLevel.VENDOR


def test_a_level_with_no_evidence_behind_it_is_replaced(submitter, reviewer, system):
    """This test used to be ``test_validation_level_never_downgrades``.

    ``upgrade_level_if_higher`` made a listing's tier sticky: once set it only ever
    rose. That is gone. A tier is now derived from the attestations behind it, so a
    value written by hand with nothing supporting it is replaced by what the
    evidence actually says - here, one community run.

    Levels going down is the point, not a regression: it is what lets an
    abandoned listing stop advertising a tier it no longer earns.
    """
    system.validation_level = ValidationLevel.ALMALINUX
    system.save()
    run = _passing_validate_run(submitter, system, level=ValidationLevel.COMMUNITY)
    services.approve_run(release(run), by=reviewer)
    system.refresh_from_db()
    assert system.validation_level == ValidationLevel.COMMUNITY


# --- component linkage (custom builds) ------------------------------------------


@pytest.fixture
def board_listing(vendor):
    from lumina.hardware.models import Component, ComponentKind
    from lumina.vendors.models import Vendor

    asrock, _ = Vendor.objects.get_or_create(name="ASRock")
    return Component.objects.create(
        name="B650M PG Riptide", vendor=asrock, owner_vendor=vendor,
        kind=ComponentKind.motherboard.value,
    )


@pytest.fixture
def cpu_listing(vendor):
    """The seeded family the custom build's Ryzen 9 7950X rolls up to."""
    from lumina.hardware.models import Component

    family = Component.objects.get(name="AMD Ryzen 7000 Series")
    family.owner_vendor = vendor
    family.save(update_fields=["owner_vendor"])
    return family


def _custom_build_run(submitter, components):
    report = f.make_report(
        run_types=["validate"],
        results=[f.validate_result("validate.cpu.functional")],
        inventory=f.custom_build_inventory(),
    )
    run = ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(f.build_bundle(report)),
        source="api",
    )
    run.listing_components.set(components)
    return release(run)


def test_custom_build_run_attests_each_linked_component(
    submitter, reviewer, board_listing, cpu_listing
):
    run = _custom_build_run(submitter, [board_listing, cpu_listing])
    services.approve_run(release(run), by=reviewer)

    board_listing.refresh_from_db()
    cpu_listing.refresh_from_db()
    assert board_listing.attestation_count == 1
    # the CPU rolled up to its family - one tie, not one per SKU
    assert cpu_listing.attestation_count == 1
    assert run.listing_components.filter(kind="cpu").count() == 1
    assert board_listing.published is True
    # board + matched CPU + the L40S GPU tied automatically from inventory
    assert CommunityAttestation.objects.filter(test_run=run).count() == 4
    assert CommunityAttestation.objects.filter(
        test_run=run, listing_component=board_listing
    ).exists()
    gpu = run.listing_components.get(kind="gpu")
    assert gpu.name == "L40S"
    assert gpu.attributes["driver_version"] == "570.86.15"


def test_component_attestations_dedupe_per_submitter(
    submitter, reviewer, board_listing
):
    first = _custom_build_run(submitter, [board_listing])
    services.approve_run(release(first), by=reviewer)
    second = _custom_build_run(submitter, [board_listing])
    services.approve_run(release(second), by=reviewer)

    board_listing.refresh_from_db()
    assert board_listing.attestation_count == 1
    assert CommunityAttestation.objects.filter(
        listing_component=board_listing
    ).count() == 1


def test_run_can_attest_system_and_components_together(
    submitter, reviewer, system, cpu_listing
):
    run = _custom_build_run(submitter, [cpu_listing])
    run.listing_system = system
    run.save()
    services.approve_run(release(run), by=reviewer)

    system.refresh_from_db()
    cpu_listing.refresh_from_db()
    assert system.attestation_count == 1
    assert cpu_listing.attestation_count == 1
    # system + board + CPU + the inventory's GPU
    # The system plus board, CPU, GPU, and NIC.
    assert CommunityAttestation.objects.filter(test_run=run).count() == 5
    gpu = run.listing_components.get(kind="gpu")
    assert gpu in system.related_components.all()
    board = run.listing_components.get(kind="motherboard")
    assert board in system.related_components.all()


def test_failing_custom_build_run_attests_nothing(submitter, reviewer, board_listing):
    report = f.make_report(
        run_types=["validate"],
        results=[f.validate_result("validate.cpu.functional", status="fail")],
        inventory=f.custom_build_inventory(),
    )
    run = ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(f.build_bundle(report)),
        source="api",
    )
    run.listing_components.set([board_listing])
    services.approve_run(release(run), by=reviewer)
    board_listing.refresh_from_db()
    assert board_listing.attestation_count == 0


# --- getting a run onto the catalog after the fact ---------------------------


def _approved_prebuilt_run(submitter, reviewer):
    report = f.make_report(
        run_types=["validate"],
        results=[f.validate_result("validate.cpu.functional")],
    )
    run = ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(f.build_bundle(report)),
        source="api",
    )
    services.approve_run(release(run), by=reviewer)
    return run


def test_assigning_after_approval_applies_the_coupling(submitter, reviewer, system):
    """The workflow gap a real reviewer hit: approve first, link second.
    The listing must still gain the attestation and get published."""
    run = _approved_prebuilt_run(submitter, reviewer)
    assert system.attestation_count == 0

    services.assign_listing(run, system=system, by=reviewer)

    system.refresh_from_db()
    assert system.attestation_count == 1
    assert system.published is True
    assert CommunityAttestation.objects.filter(
        test_run=run, listing_system=system
    ).count() == 1


def test_assigning_after_approval_still_dedupes(submitter, reviewer, system):
    run = _approved_prebuilt_run(submitter, reviewer)
    services.assign_listing(run, system=system, by=reviewer)
    services.assign_listing(run, system=system, by=reviewer)  # reviewer re-saves
    system.refresh_from_db()
    assert system.attestation_count == 1
    assert CommunityAttestation.objects.filter(
        test_run=run, listing_system=system
    ).count() == 1


def test_create_listings_from_prebuilt_run(submitter, reviewer):
    """A System listing is created from the run's own DMI identity, linked,
    published, and attested in one action."""
    from lumina.hardware.models import System

    run = _approved_prebuilt_run(submitter, reviewer)
    listings = services.create_listings_from_run(run, by=reviewer)

    assert len(listings) == 1
    system = System.objects.get(name="PowerEdge R760")
    assert system.vendor.name == "Dell Inc."
    assert system.published is True
    assert system.attestation_count == 1
    run.refresh_from_db()
    assert run.listing_system == system


def test_create_listings_from_prebuilt_reuses_existing_listing(
    submitter, reviewer, vendor
):
    from lumina.hardware.models import System
    from lumina.vendors.models import Vendor

    dell, _ = Vendor.objects.get_or_create(name="Dell Inc.")
    existing = System.objects.create(name="PowerEdge R760", vendor=dell)
    run = _approved_prebuilt_run(submitter, reviewer)

    listings = services.create_listings_from_run(run, by=reviewer)

    assert listings == [existing]
    assert System.objects.filter(name="PowerEdge R760").count() == 1


def test_create_listings_from_custom_build_run(submitter, reviewer):
    """A custom build creates motherboard + CPU components, mapping the
    CPUID vendor string to the brand name."""
    from lumina.hardware.models import ComponentKind

    report = f.make_report(
        run_types=["validate"],
        results=[f.validate_result("validate.cpu.functional")],
        inventory=f.custom_build_inventory(),
    )
    run = ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(f.build_bundle(report)),
        source="api",
    )
    services.approve_run(release(run), by=reviewer)

    listings = services.create_listings_from_run(run, by=reviewer)

    # motherboard + CPU family + the GPU the inventory recorded
    assert len(listings) == 3
    board = run.listing_components.get(kind=ComponentKind.motherboard.value)
    assert board.name == "B650M PG Riptide"
    assert board.vendor.name == "ASRock"
    cpu = run.listing_components.get(kind=ComponentKind.cpu.value)
    assert cpu.vendor.name == "AMD"  # AuthenticAMD mapped to the brand
    gpu = run.listing_components.get(kind=ComponentKind.gpu.value)
    assert gpu.vendor.name == "NVIDIA"
    assert gpu.name == "L40S"
    # kind-specific details ride in the generic attributes field
    assert gpu.attributes["driver"] == "nvidia"
    assert gpu.attributes["driver_version"] == "570.86.15"
    assert board.attestation_count == 1
    assert cpu.attestation_count == 1
    assert gpu.attestation_count == 1
    # Board, CPU, GPU, NIC - the NIC arrived once the collector could name it.
    assert run.listing_components.count() == 4


def test_create_listings_refuses_unknown_kind(submitter, reviewer):
    inventory = f.default_inventory()
    # Unknown by its inputs rather than by declaration: no system product, and a board whose
    # manufacturer is a placeholder too, so nothing names this machine.
    inventory["summary"]["system"].update({"vendor": None, "product": None})
    inventory["summary"]["baseboard"] = {"vendor": "OEM", "product": "0123456789"}
    report = f.make_report(inventory=inventory)
    run = ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(f.build_bundle(report)),
        source="api",
    )
    with pytest.raises(services.ReviewError):
        services.create_listings_from_run(run, by=reviewer)
