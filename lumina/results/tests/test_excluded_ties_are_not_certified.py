"""An excluded part must not reach the catalog, by any path, on any machine kind.

The report: a BMC display adapter blacklisted by the categorical management-display rule kept
coming back as a certified GPU. It was unticked at ingest, recorded in
``excluded_component_ties``, shown as "not attached" on the review screen, and published anyway,
every time the listing was cleaned up.

Two causes, one per half of this file.

**The custom-build branch of ``create_listings_from_run`` tied the parts itself.** A second
implementation of ``ensure_component_ties``, reached moments later through the same approval,
that read the board, the CPU, and every driver-bound GPU straight off the run and never looked
at the exclusions. It only ever fired for ``effective_system_kind == custom``, which is why a
prebuilt machine with the same adapter behaved.

**Ties only accumulated.** Nothing ever removed one, so a part attached before its rule existed,
or by the branch above, stayed attached and kept collecting attestations - the rule took effect
on future runs and never on the run in front of the reviewer.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import Group, User

from lumina.hardware.models import ComponentExclusionRule, ComponentKind
from lumina.results import ingest, services
from lumina.results.models import TestRun
from lumina.results.tests import factories as f
from lumina.results.tests.helpers import release

pytestmark = pytest.mark.django_db

# The real device from the report, and the reason it is the one in this test: an ASPEED display
# adapter is the BMC's, present on essentially every server board, and of no interest to anybody
# reading the catalog for graphics hardware.
ASPEED = {"pci": "0c:00.0", "class": "VGA compatible controller [0300]", "class_id": "0300",
          "pci_ids": {"vendor": "ASPEED Technology, Inc. [1a03]",
                      "device": "ASPEED Graphics Family [2000]"}, "driver": "ast"}
NVIDIA = {"pci": "81:00.0", "class": "VGA compatible controller [0300]", "class_id": "0300",
          "pci_ids": {"vendor": "NVIDIA Corporation [10de]",
                      "device": "AD102GL [L40S] [26b9]"}, "driver": "nvidia"}


@pytest.fixture
def submitter():
    return User.objects.create_user("excl-sub")


@pytest.fixture
def reviewer():
    user = User.objects.create_user("excl-rev")
    user.groups.add(Group.objects.get_or_create(name="reviewer")[0])
    return user


def _custom_run(submitter, pci_devices=(ASPEED, NVIDIA)) -> TestRun:
    """A passing validate run on a self-built machine carrying the given PCI devices."""
    inventory = f.custom_build_inventory()
    inventory["summary"]["gpus"] = []
    inventory["summary"]["nics"] = []
    inventory["summary"]["pci_devices"] = list(pci_devices)
    run = ingest.ingest_bundle(
        submitter=submitter, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"], inventory=inventory,
            results=[f.validate_result("validate.cpu.functional")],
        ))),
    )
    return release(TestRun.objects.get(pk=run.pk))


def _tied_names(run) -> set[str]:
    return {component.name for component in run.listing_components.all()}


# --- the custom-build branch ------------------------------------------------------


def test_a_blacklisted_adapter_is_not_tied_by_a_custom_build(submitter, reviewer):
    """The reported bug, end to end: approve a custom build, create its listings, and the
    excluded adapter must be attached to nothing."""
    run = _custom_run(submitter)
    assert "gpu:aspeed graphics family" in run.excluded_component_ties

    services.approve_run(run, by=reviewer)
    services.create_listings_from_run(run, by=reviewer)

    run.refresh_from_db()
    assert "ASPEED Graphics Family" not in _tied_names(run)
    # Not merely untied: it is not in the catalog as a certified part at all.
    assert not run.listing_system.related_components.filter(
        name="ASPEED Graphics Family").exists()
    # The card somebody actually bought is still certified, so this is an exclusion and not an
    # outage: a test that only asserted the absence would pass with GPU ties broken entirely.
    assert "L40S" in _tied_names(run)


def test_the_board_is_still_the_listing_a_custom_build_creates(submitter, reviewer):
    """Dropping those loops must not drop the listing. The board is not a tie - it is the
    thing being created, and what ``_ensure_custom_system`` promotes to a machine."""
    run = _custom_run(submitter)
    services.approve_run(run, by=reviewer)

    listings = services.create_listings_from_run(run, by=reviewer)

    assert [listing.name for listing in listings] == ["B650M PG Riptide"]


def test_a_prebuilt_machine_behaves_the_same(submitter, reviewer):
    """Named rather than assumed: the bug was in one branch, so the other branch's behaviour is
    the control that says the exclusion itself was always being honored somewhere."""
    inventory = f.default_inventory()
    inventory["summary"]["gpus"] = []
    inventory["summary"]["nics"] = []
    inventory["summary"]["pci_devices"] = [ASPEED, NVIDIA]
    run = release(TestRun.objects.get(pk=ingest.ingest_bundle(
        submitter=submitter, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"], inventory=inventory,
            results=[f.validate_result("validate.cpu.functional")],
        ))),
    ).pk))
    services.approve_run(run, by=reviewer)
    services.create_listings_from_run(run, by=reviewer)

    assert "ASPEED Graphics Family" not in _tied_names(run)


# --- reconciling a tie that predates the rule --------------------------------------


def test_a_rule_written_later_unties_what_it_covers(submitter, reviewer):
    """A part tied while it was allowed is detached the next time ties are ensured.

    Without this the reviewer's only remedy is the admin, on every listing the part ever
    reached, and the rule they just wrote does nothing about the run they wrote it from.
    """
    run = _custom_run(submitter, pci_devices=[NVIDIA])
    services.approve_run(run, by=reviewer)
    services.create_listings_from_run(run, by=reviewer)
    assert "L40S" in _tied_names(run)

    ComponentExclusionRule.objects.create(
        vendor_id="10de", device_id="26b9", kind=ComponentKind.gpu.value,
        reason="lab card, not for the catalog",
    )
    services.seed_rule_exclusions(run)
    services.ensure_component_ties(run)

    run.refresh_from_db()
    assert "L40S" not in _tied_names(run)
    assert not run.listing_system.related_components.filter(name="L40S").exists()


def test_untying_is_audit_logged(submitter, reviewer):
    """Somebody has to be able to answer "why did this part leave the listing", and a silent
    detach on approval is exactly the kind of change that gets blamed on the wrong thing."""
    from lumina.audit.models import AuditLogEntry

    run = _custom_run(submitter, pci_devices=[NVIDIA])
    services.approve_run(run, by=reviewer)
    services.create_listings_from_run(run, by=reviewer)
    ComponentExclusionRule.objects.create(
        vendor_id="10de", device_id="26b9", kind=ComponentKind.gpu.value, reason="lab card")
    services.seed_rule_exclusions(run)

    services.ensure_component_ties(run)

    entry = AuditLogEntry.objects.get(action="test_run.component_untied")
    assert entry.after["reason"] == "lab card"
    assert "L40S" in entry.after["component"]


def test_an_excluded_cpu_leaves_the_machine_cpu_set(submitter, reviewer):
    """A CPU ties into ``System.cpus`` rather than the related components, so untying it from
    only the one set would leave the machine still listing it as a certified processor."""
    run = _custom_run(submitter, pci_devices=[NVIDIA])
    services.approve_run(run, by=reviewer)
    services.create_listings_from_run(run, by=reviewer)
    cpu = run.listing_components.get(kind=ComponentKind.cpu.value)
    assert run.listing_system.cpus.filter(pk=cpu.pk).exists()

    run.excluded_component_ties = [
        target["key"] for target in services.component_tie_targets(run)
        if target["kind"] == ComponentKind.cpu
    ]
    run.save(update_fields=["excluded_component_ties"])
    services.ensure_component_ties(run)

    assert not run.listing_system.cpus.filter(pk=cpu.pk).exists()
    assert not run.listing_components.filter(pk=cpu.pk).exists()


def test_a_part_the_catalog_never_knew_is_not_created_to_be_removed(submitter, reviewer):
    """``_untie_excluded`` matches, never creates. An excluded part that was never tied has no
    component row, and manufacturing one so it can be detached would put the blacklisted model
    in the catalog - which is the whole thing being prevented."""
    from lumina.hardware.models import Component

    run = _custom_run(submitter)
    services.approve_run(run, by=reviewer)
    services.create_listings_from_run(run, by=reviewer)

    assert not Component.objects.filter(name="ASPEED Graphics Family").exists()


# --- resolving a tie the way it was made ------------------------------------------
#
# Two Navi 31 cards, which the seeded "AMD Radeon RX 7000 Series (RDNA 3)" family covers. The
# family is the seeded one rather than one this file defines: what matters is that a tie lands
# on a family at all, and a fixture family would only prove that a fixture family works.

AMD_GPU = {"pci": "03:00.0", "class": "VGA compatible controller [0300]", "class_id": "0300",
           "pci_ids": {"vendor": "Advanced Micro Devices, Inc. [1002]",
                       "device": "Navi 31 [Radeon RX 7900 XTX] [744c]"}, "driver": "amdgpu"}
AMD_GPU_2 = {"pci": "04:00.0", "class": "VGA compatible controller [0300]", "class_id": "0300",
             "pci_ids": {"vendor": "Advanced Micro Devices, Inc. [1002]",
                         "device": "Navi 31 [Radeon RX 7900 XT] [744d]"}, "driver": "amdgpu"}


def _amd_alias():
    """Teach the catalog that the brand in an AMD PCI id is its "AMD" vendor.

    Without it ``Advanced Micro Devices, Inc.`` forks a vendor of its own, which has no curated
    families, and the cards resolve at model level - so the rollup these two tests are about
    never happens and they pass while asserting nothing.
    """
    from lumina.vendors.models import Vendor, VendorAlias

    vendor, _ = Vendor.objects.get_or_create(
        name="AMD", defaults={"slug": "amd", "published": True})
    VendorAlias.objects.get_or_create(
        name="Advanced Micro Devices, Inc.", defaults={"vendor": vendor})


def _gpu_tie(run):
    from lumina.hardware.models import ComponentRole

    component = run.listing_components.get(kind=ComponentKind.gpu.value)
    assert component.role == ComponentRole.FAMILY, (
        "these cards must roll up to a family or the test proves nothing")
    return component


def test_an_excluded_part_that_rolled_up_to_a_family_is_found(submitter, reviewer):
    """Certification is granted per family, so a tie lands on the family entry. Undoing it has
    to look there too: matching the excluded model at model level finds nothing, leaves the
    family tied, and the part stays certified while the screen says it is not attached."""
    _amd_alias()
    run = _custom_run(submitter, pci_devices=[AMD_GPU])
    services.approve_run(run, by=reviewer)
    services.create_listings_from_run(run, by=reviewer)
    family = _gpu_tie(run)

    run.excluded_component_ties = [
        target["key"] for target in services.component_tie_targets(run)
        if target["kind"] == ComponentKind.gpu
    ]
    run.save(update_fields=["excluded_component_ties"])
    services.ensure_component_ties(run)

    assert not run.listing_components.filter(pk=family.pk).exists()


def test_excluding_one_card_keeps_a_family_its_sibling_still_earns(submitter, reviewer):
    """Two cards of one family are one catalog entry. Untying the excluded one must not take
    away the entry the other card is evidence for - the exclusion says nothing about it."""
    _amd_alias()
    run = _custom_run(submitter, pci_devices=[AMD_GPU, AMD_GPU_2])
    services.approve_run(run, by=reviewer)
    services.create_listings_from_run(run, by=reviewer)
    family = _gpu_tie(run)

    run.excluded_component_ties = [
        target["key"] for target in services.component_tie_targets(run)
        if "7900 XTX" in target["raw_model"]
    ]
    assert run.excluded_component_ties, "the XTX has to be one of the targets"
    run.save(update_fields=["excluded_component_ties"])
    services.ensure_component_ties(run)

    assert run.listing_components.filter(pk=family.pk).exists()
