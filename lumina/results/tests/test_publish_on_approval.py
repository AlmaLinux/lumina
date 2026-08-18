"""Approving a run publishes everything it is evidence for, and repairing the ones it did not.

The invariant first, because nothing asserted it and that is how the fault below shipped:
approving a passing whole-machine validation run puts the machine **and every part it ties** in
the catalog. A submitter watches a reviewer approve their run and expects to find the board, the
CPU family, the GPUs, and the NICs there afterwards.

Then the repair. ``apply_run_certification`` used to record certification before the components
were tied, so a part whose first-ever run was that one attested to nothing and stayed unpublished
while the machine around it published normally. The ordering is fixed; the rows it left behind are
not, and they surface on a submitter's dashboard as "unpublished" beside runs they saw approved.
"""
from __future__ import annotations

from io import StringIO

import pytest
from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.utils import timezone

from lumina.hardware.models import Component, ComponentKind, ListingVersion, System
from lumina.releases.models import AlmaLinuxRelease
from lumina.results import ingest, services
from lumina.results.models import RunType, TestRun
from lumina.results.tests import factories as f
from lumina.vendors.models import Vendor

pytestmark = pytest.mark.django_db


@pytest.fixture
def release():
    obj, _ = AlmaLinuxRelease.objects.get_or_create(major=9, defaults={"supported": True})
    return obj


@pytest.fixture
def submitter():
    return User.objects.create_user("pub-sub", email="pubsub@example.com")


@pytest.fixture
def reviewer():
    return User.objects.create_user("pub-rev", email="pubrev@example.com", is_superuser=True)


def approved_run(submitter, reviewer, release):
    """A whole-machine validation run, taken through the real approval."""
    run = ingest.ingest_bundle(
        submitter=submitter, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            results=[f.validate_result("validate.cpu.functional")],
        ))),
    )
    run.status = TestRun.STATUS_PENDING
    run.save(update_fields=["status"])
    services.approve_run(run, by=reviewer)
    run.refresh_from_db()
    return run


def repair(*args) -> str:
    out = StringIO()
    call_command("reapply_run_certification", *args, stdout=out)
    return out.getvalue()


# --- the invariant --------------------------------------------------------------------


def test_approving_a_run_publishes_every_part_it_ties(submitter, reviewer, release):
    """Nothing asserted this, and it is the whole promise of tying parts to a run: the
    machine reaching the catalog without its board, CPU, GPU, and NIC is a half-published
    machine that reads to its submitter as work they still owe."""
    run = approved_run(submitter, reviewer, release)

    tied = list(run.listing_components.all())
    assert {component.kind for component in tied} == {"motherboard", "cpu", "gpu", "nic"}
    assert run.listing_system.published
    assert [component for component in tied if not component.published] == []


def test_every_part_gets_the_release_row_its_attestation_hangs_off(submitter, reviewer, release):
    """The ordering the repair below exists for. Certification applied before the parts were
    tied left them with no ``ListingVersion``, and an attestation hangs off that row - so the
    part published nothing and nothing said why."""
    run = approved_run(submitter, reviewer, release)

    for component in run.listing_components.all():
        assert ListingVersion.objects.filter(
            release=release, listing_component=component).exists()


# --- the repair -----------------------------------------------------------------------


def stalled(submitter, release, *, published_at=True) -> tuple[TestRun, Component]:
    """An approved run tied to a part that was never published: the state the fault left."""
    # get_or_create: the reference-data migration seeds the silicon vendors.
    vendor, _ = Vendor.objects.get_or_create(name="NVIDIA", defaults={"published": True})
    component = Component.objects.create(
        vendor=vendor, name="L40S", kind=ComponentKind.gpu.value, created_by=submitter,
    )
    run = TestRun.objects.create(
        run_type=RunType.validate.value, schema_version="1.0", suite_version="0.1.0",
        submitter=submitter, source="api",
        bundle=ContentFile(b"x", name=f"b{TestRun.objects.count()}.tar.zst"),
        bundle_sha256=f"{TestRun.objects.count():064d}",
        status=TestRun.STATUS_APPROVED, alma_release=release, alma_minor=6,
        host_os_id="almalinux",
        published_at=timezone.now() if published_at else None,
    )
    run.listing_components.add(component)
    return run, component


def test_it_publishes_what_an_approval_left_behind(submitter, release):
    run, component = stalled(submitter, release)

    output = repair()

    component.refresh_from_db()
    assert component.published
    assert str(run.uuid) in output and "L40S" in output


def test_it_records_the_attestation_the_approval_should_have(submitter, release):
    """Publishing the row alone would leave a listing in the catalog with nothing behind it."""
    _run, component = stalled(submitter, release)

    repair()

    version = ListingVersion.objects.get(release=release, listing_component=component)
    assert version.attestations.exists()


def test_a_second_pass_finds_nothing(submitter, release):
    """It is run by hand on a live host, so somebody will run it twice."""
    stalled(submitter, release)
    repair()

    assert "Nothing to repair" in repair()


def test_a_healthy_database_is_untouched(submitter, reviewer, release):
    approved_run(submitter, reviewer, release)

    assert "Nothing to repair" in repair()


def test_a_dry_run_writes_nothing(submitter, release):
    _run, component = stalled(submitter, release)

    output = repair("--dry-run")

    component.refresh_from_db()
    assert not component.published
    assert "Would publish" in output


def test_an_embargoed_run_is_left_alone(submitter, release):
    """Approved and withheld on purpose. ``publish_due_runs`` releases it on the day, and
    repairing it early would put unreleased hardware in the catalog - the one thing the
    embargo exists to prevent."""
    _run, component = stalled(submitter, release, published_at=False)

    output = repair()

    component.refresh_from_db()
    assert not component.published
    assert "Nothing to repair" in output


def test_it_ties_nothing_new(submitter, release):
    """It records what the run's existing ties earned and resolves no part again. A naming
    rule or an exclusion written since the approval would resolve the machine's hardware
    differently, and a repair that quietly refiles it under new names is worse than the
    fault it is fixing."""
    run, _component = stalled(submitter, release)
    run.board_vendor, run.board_model = "Dell Inc.", "0M83RH"
    run.save(update_fields=["board_vendor", "board_model"])

    repair()

    assert not Component.objects.filter(name__icontains="0M83RH").exists()
    assert run.listing_components.count() == 1


def test_it_reports_each_part_once_however_many_runs_tie_it(submitter, release):
    """A machine validated on three releases ties its GPU three times; the repair publishes
    it on the first and the other two still carry a prefetched row saying it is unpublished."""
    run, component = stalled(submitter, release)
    second = TestRun.objects.create(
        run_type=RunType.validate.value, schema_version="1.0", suite_version="0.1.0",
        submitter=submitter, source="api",
        bundle=ContentFile(b"y", name="b-second.tar.zst"),
        bundle_sha256=f"{1:064d}", status=TestRun.STATUS_APPROVED,
        alma_release=release, alma_minor=6, host_os_id="almalinux",
        published_at=timezone.now(),
    )
    second.listing_components.add(component)
    assert run.pk != second.pk

    output = repair()

    assert output.count("L40S") == 1
    assert "1 listing(s)" in output


def test_a_system_left_behind_is_repaired_too(submitter, release):
    """Not only components. A machine whose own listing failed to publish is the same fault."""
    vendor, _ = Vendor.objects.get_or_create(name="Dell Inc.", defaults={"published": True})
    system = System.objects.create(vendor=vendor, name="PowerEdge R760", created_by=submitter)
    run = TestRun.objects.create(
        run_type=RunType.validate.value, schema_version="1.0", suite_version="0.1.0",
        submitter=submitter, source="api",
        bundle=ContentFile(b"z", name="b-sys.tar.zst"), bundle_sha256=f"{2:064d}",
        status=TestRun.STATUS_APPROVED, alma_release=release, alma_minor=6,
        host_os_id="almalinux", published_at=timezone.now(), listing_system=system,
    )
    assert run.pk

    repair()

    system.refresh_from_db()
    assert system.published
