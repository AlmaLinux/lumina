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
    assert "Would try" in output


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


# --- a Kitten run certifies the major it ran on -----------------------------------
#
# Reported from production: six components of one machine stayed drafts after an approved run,
# and the repair command said it had published them. Both halves came from one line in
# ``record_compatibility`` that required ``alma_minor`` as well as a release - and AlmaLinux
# Kitten has no minor, so it wrote no release row, and certification then had nothing to hang an
# attestation on and gave up without saying so.


def kitten_run(submitter, release) -> TestRun:
    """An approved, released whole-machine run on Kitten: a release, no minor."""
    vendor, _ = Vendor.objects.get_or_create(name="LENOVO", defaults={"published": True})
    system = System.objects.create(vendor=vendor, name="ThinkPad P16 Gen 3")
    component = Component.objects.create(
        vendor=vendor, name="21RRZD7QUS", kind=ComponentKind.motherboard.value,
    )
    run = TestRun.objects.create(
        run_type=RunType.validate.value, schema_version="1.0", suite_version="0.1.0",
        submitter=submitter, source="api",
        bundle=ContentFile(b"k", name="kitten.tar.zst"), bundle_sha256=f"{9:064d}",
        status=TestRun.STATUS_APPROVED, alma_release=release, alma_minor=None,
        host_os_id="almalinux", published_at=timezone.now(), listing_system=system,
    )
    run.listing_components.add(component)
    return run


def test_a_kitten_run_certifies_the_major_it_ran_on(submitter, release):
    """The reported bug. Kitten has no minor, and the major is the whole claim: a run that
    passed on Kitten 10 is evidence about AlmaLinux 10."""
    run = kitten_run(submitter, release)

    services.apply_run_certification(run, tie=False)

    assert run.listing_system.__class__.objects.get(pk=run.listing_system_id).published
    assert all(c.published for c in Component.objects.filter(test_runs=run))


def test_it_records_a_release_row_without_a_minor(submitter, release):
    """The minor is provenance on the run, not the scope of the claim - which is what
    ``record_compatibility``'s own docstring has said since the floor was removed."""
    run = kitten_run(submitter, release)

    services.apply_run_certification(run, tie=False)

    version = ListingVersion.objects.get(release=release, listing_system=run.listing_system)
    assert version.source == ListingVersion.SOURCE_RUN
    assert version.available_from_minor is None, "no --support-from-minor was given"


def test_the_repair_command_reports_what_actually_happened(submitter, release):
    """It reported what it intended to publish and never looked at the result, so a repair that
    did nothing printed six names and a success. That is how this went unnoticed twice."""
    kitten_run(submitter, release)

    output = repair()

    assert "Published" in output
    assert "did not publish" not in output


def test_a_certification_that_publishes_nothing_leaves_a_trace(submitter, release):
    """The whole incident was invisible: no audit entry, no Sentry event, nothing but a status
    column on one person's dashboard."""
    from lumina.audit.models import AuditLogEntry

    run = kitten_run(submitter, release)
    # A validate run naming a release this catalog does not know, so there is no major to make
    # a statement about and nothing publishes. A real state, and the one the audit entry is for.
    run.alma_release = None
    run.save(update_fields=["alma_release"])

    services.apply_run_certification(run, tie=False)

    assert AuditLogEntry.objects.filter(
        action="test_run.certification_incomplete", target_id=str(run.pk),
    ).exists()


def test_record_compatibility_writes_a_row_for_a_kitten_run(submitter, release):
    """Directly, because the two fixes mask each other end to end: certification now ensures
    its own release row, so a Kitten run publishes even with the old minor gate back in place.
    This is the one that sees the gate."""
    run = kitten_run(submitter, release)

    recorded = services.record_compatibility(run)

    assert recorded, "a Kitten run recorded no compatibility at all"
    assert {entry["release"] for entry in recorded} == {release.major}


def test_attestation_alone_publishes_without_a_prior_compatibility_pass(submitter, release):
    """The structural half. ``_apply_attestation`` is reached from three places that do not
    create release rows first - ``create_listings_from_run`` twice, and the re-assess path - and
    it used to give up silently when the row was missing. It makes the row now."""
    run = kitten_run(submitter, release)

    services._apply_attestation(run)

    assert System.objects.get(pk=run.listing_system_id).published


def test_a_benchmark_approval_is_not_reported_as_incomplete(submitter, release):
    """A benchmark run certifies nothing by design and is approved constantly. Logging those
    would bury the one entry worth reading."""
    from lumina.audit.models import AuditLogEntry

    run = kitten_run(submitter, release)
    run.run_type = RunType.benchmark.value
    run.save(update_fields=["run_type"])

    services.apply_run_certification(run, tie=False)

    assert not AuditLogEntry.objects.filter(
        action="test_run.certification_incomplete", target_id=str(run.pk)).exists()


def test_the_command_says_so_when_a_repair_does_not_take(submitter, release):
    """The failure that started this: it reported six names and a success having published
    nothing. A run naming no release cannot certify, and the command has to say that rather
    than claim it worked."""
    run = kitten_run(submitter, release)
    run.alma_release = None
    run.save(update_fields=["alma_release"])

    output = repair()

    assert "did not publish" in output
    assert "Published" not in output
