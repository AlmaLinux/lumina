"""NICs become catalog components like anything else the run exercised.

Reported: "I notice the NIC is not showing up as a component. Why is that? We should be detecting
NICs."

Detection was never the problem - ``ip -j link`` enumerated every physical interface, with its
driver, MAC, PCI slot, and firmware. None of that names a *part*. "enp2s0 running r8169" is a fact
about this kernel's view of the machine, not a product anybody can look up or certify, so there
was nothing to build a catalog entry from.

Two changes, one in each repository: the collector joins ``lspci -vmmnnk`` for the vendor and
device strings, the way the GPU collector always has, and ``component_tie_targets`` emits the
result. The suite half is covered by ``tests/test_nic_identity.py`` over there.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import Group, User
from django.urls import reverse

from lumina.hardware.models import Component, ComponentKind
from lumina.releases.models import AlmaLinuxRelease
from lumina.results import ingest, services
from lumina.results.component_match import normalize_nic_model
from lumina.results.models import TestRun
from lumina.results.pci_names import nic_identity, pci_name
from lumina.results.tests import factories as f
from lumina.results.tests.helpers import release as ready

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def release():
    AlmaLinuxRelease.objects.get_or_create(major=9, defaults={"supported": True})


@pytest.fixture
def submitter():
    return User.objects.create_user("nic-sub", password="pw")


@pytest.fixture
def reviewer():
    user = User.objects.create_user("nic-rev", password="pw")
    group, _ = Group.objects.get_or_create(name="reviewer")
    user.groups.add(group)
    return user


def _run(submitter, nics=None):
    inventory = f.default_inventory()
    if nics is not None:
        inventory["summary"]["nics"] = nics
    run = ingest.ingest_bundle(
        submitter=submitter, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"], inventory=inventory,
            results=[f.validate_result("validate.cpu.functional")],
        ))),
    )
    return TestRun.objects.get(pk=run.pk)


def _kinds(run):
    return [target["kind"] for target in services.component_tie_targets(run)]


# --- they appear at all -----------------------------------------------------------


def test_a_named_nic_becomes_a_tie_target(submitter):
    run = _run(submitter)

    assert ComponentKind.nic in _kinds(run)


def test_the_target_carries_the_reported_identity(submitter):
    run = _run(submitter)

    nic = next(
        t for t in services.component_tie_targets(run) if t["kind"] == ComponentKind.nic
    )

    assert nic["brand"] == "Broadcom Inc. and subsidiaries"
    assert nic["raw_model"].startswith("BCM57414")
    assert nic["attributes"]["driver"] == "bnxt_en"


def test_approving_creates_the_component(submitter, reviewer):
    run = _run(submitter)

    services.approve_run(ready(run), by=reviewer)

    run.refresh_from_db()
    nic = run.listing_components.get(kind=ComponentKind.nic.value)
    assert nic.name == normalize_nic_model(
        "BCM57414 NetXtreme-E 10Gb/25Gb RDMA Ethernet Controller"
    )
    assert nic.vendor.name  # resolved through the alias table, not a hardcoded map


def test_the_submitter_sees_a_row_for_it(client, submitter):
    run = _run(submitter)
    client.force_login(submitter)

    body = client.get(reverse("results:propose_listing", args=[run.uuid])).content.decode()

    assert "BCM57414" in body
    assert "NIC" in body, "its kind label"


# --- one part, not one device -----------------------------------------------------


def test_two_ports_of_one_card_are_one_component(submitter):
    """The default fixture reports both ports of a dual-port card.

    My first version of this claimed ``tie_key`` handled the collapse on its own. It does not:
    the keys match, and nothing was dropping the duplicate - so the form showed two rows for one
    card, with two checkboxes sharing a single ``included_ties`` value, and the summary read as
    two pieces of evidence.
    """
    run = _run(submitter)

    nics = [t for t in services.component_tie_targets(run) if t["kind"] == ComponentKind.nic]

    assert len(nics) == 1


def test_two_different_cards_are_two_components(submitter):
    """The dedup must key on the part, not the kind."""
    run = _run(submitter, nics=[
        {"name": "eno1", "vendor": "Intel Corporation", "driver": "i40e",
         "model": "Ethernet Controller X710 for 10GbE SFP+"},
        {"name": "eno2", "vendor": "Intel Corporation", "driver": "igb",
         "model": "I350 Gigabit Network Connection"},
    ])

    nics = [t for t in services.component_tie_targets(run) if t["kind"] == ComponentKind.nic]

    assert len(nics) == 2


def test_duplicate_gpus_collapse_too(submitter):
    """The same latent bug existed for two identical cards long before NICs were emitted, which
    is why the fix is in ``component_tie_targets`` rather than in the NIC branch."""
    inventory = f.default_inventory()
    gpu = dict(inventory["summary"]["gpus"][0])
    inventory["summary"]["gpus"] = [gpu, {**gpu, "pci": "0000:cb:00.0"}]
    run = ingest.ingest_bundle(
        submitter=submitter, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"], inventory=inventory,
            results=[f.validate_result("validate.cpu.functional")],
        ))),
    )

    gpus = [
        t for t in services.component_tie_targets(TestRun.objects.get(pk=run.pk))
        if t["kind"] == ComponentKind.gpu
    ]

    assert len(gpus) == 1


# --- what does not become a component ---------------------------------------------


def test_a_driverless_nic_is_not_catalogued(submitter):
    """The same rule ``tieable_gpus`` applies, and it matters more here. A port with no driver is
    a PCI device that answered on the bus: nothing configured it, no test moved a packet through
    it, and cataloguing it would attest hardware that did not work."""
    run = _run(submitter, nics=[
        {"name": "eno1", "vendor": "Intel Corporation",
         "model": "I350 Gigabit Network Connection", "driver": None},
    ])

    assert ComponentKind.nic not in _kinds(run)


def test_an_unnamed_nic_is_not_catalogued(submitter):
    """USB ethernet, an SoC's built-in MAC, or a machine with no lspci. Also every bundle
    submitted before the collector learned to name NICs: they tie nothing rather than tying
    something unnamed, and the next run of that machine names them."""
    run = _run(submitter, nics=[
        {"name": "usb0", "driver": "cdc_ether", "vendor": "", "model": ""},
    ])

    assert ComponentKind.nic not in _kinds(run)


def test_a_report_with_no_nics_at_all_is_fine(submitter):
    run = _run(submitter, nics=[])

    assert ComponentKind.nic not in _kinds(run)
    assert ComponentKind.cpu in _kinds(run), "and the rest still works"


# --- naming ----------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    # The tail after the part number is what pci.ids repeats on every device from that vendor.
    ("RTL8111/8168/8411 PCI Express Gigabit Ethernet Controller", "RTL8111/8168/8411"),
    ("I350 Gigabit Network Connection", "I350"),
    ("BCM57414 NetXtreme-E 10Gb/25Gb RDMA Ethernet Controller",
     "BCM57414 NetXtreme-E 10Gb/25Gb RDMA"),
    # Leading boilerplate stays. Stripping these words anywhere was the first attempt and it
    # mangled the names that lead with them: "Ethernet Converged Network Adapter X710-DA2" came
    # out as "Converged X710-DA2".
    ("Ethernet Converged Network Adapter X710-DA2",
     "Ethernet Converged Network Adapter X710-DA2"),
    ("Ethernet Controller X710 for 10GbE SFP+", "Ethernet Controller X710 for 10GbE SFP+"),
    # lspci uses the same "chip [product]" shape it uses for GPUs.
    ("MT27700 Family [ConnectX-4]", "ConnectX-4"),
    ("Wi-Fi 6 AX200", "Wi-Fi 6 AX200"),
])
def test_the_catalog_name(raw, expected):
    assert normalize_nic_model(raw) == expected


def test_the_vendor_goes_through_the_alias_table(submitter, reviewer):
    """No hardcoded vendor map, unlike CPUs and GPUs. Those have three vendors between them; NICs
    are Realtek, Broadcom, Mellanox, Marvell, Aquantia, and a long tail, so a table here would be
    permanently one entry short."""
    from lumina.vendors.models import Vendor, VendorAlias

    vendor = Vendor.objects.create(name="Broadcom", published=True)
    VendorAlias.objects.create(vendor=vendor, name="Broadcom Inc. and subsidiaries")
    run = _run(submitter)

    services.approve_run(ready(run), by=reviewer)

    run.refresh_from_db()
    nic = run.listing_components.get(kind=ComponentKind.nic.value)
    assert nic.vendor == vendor, "the long pci.ids name resolved to the catalog vendor"
    assert Component.objects.filter(kind=ComponentKind.nic.value).count() == 1


# --- the server decides which reported name is the product ------------------------
#
# Reported from a real bundle: "the realtek NIC was detected as model 'Device'". So was the Intel
# Wi-Fi beside it. lspci had named both chips perfectly well; what it had no name for was their
# *subsystem*, where it writes the placeholder "Device [09a8]" - and the collector was choosing
# the subsystem name.
#
# Then asked: "wouldn't it be cleanest to report both/all and then let the frontend have the
# deciding logic?" It is, and it is the rule this project already follows for CPU models, GPU
# models, and the Kitten marker. The bundle now carries every name lspci gave; ``nic_identity``
# chooses. So a rule that turns out wrong is corrected for every bundle ever submitted, rather
# than only for runs made after the fix ships - which is exactly what would have saved this one.

REPORTED = {
    "vendor": "Realtek Semiconductor Co., Ltd. [10ec]",
    "device": "RTL8111/8168/8211/8411 PCI Express Gigabit Ethernet Controller [8168]",
    "subsystem_vendor": "Dell [1028]",
    "subsystem_device": "Device [09a8]",
}


def test_the_reported_bundle_names_the_chip():
    """The exact ``pci_ids`` from run d4857592, which produced the model "Device"."""
    vendor, model = nic_identity({"pci_ids": REPORTED})

    assert vendor == "Realtek Semiconductor Co., Ltd."
    assert model == "RTL8111/8168/8211/8411 PCI Express Gigabit Ethernet Controller"


def test_the_wireless_in_the_same_bundle_too():
    vendor, model = nic_identity({"pci_ids": {
        "vendor": "Intel Corporation [8086]",
        "device": "Wi-Fi 6 AX200 [2723]",
        "subsystem_vendor": "Intel Corporation [8086]",
        "subsystem_device": "Device [4080]",
    }})

    assert (vendor, model) == ("Intel Corporation", "Wi-Fi 6 AX200")


def test_the_controller_wins_over_the_card_sku():
    """Device-first: the catalog names the controller, not the retail card SKU. Two differently
    branded cards built on one X710 are the same silicon to certify, and the controller is the
    string lshw also reports as the product; the SKU in ``subsystem_device`` is not preferred."""
    vendor, model = nic_identity({"pci_ids": {
        "vendor": "Intel Corporation [8086]",
        "device": "Ethernet Controller X710 for 10GbE SFP+ [1572]",
        "subsystem_device": "Ethernet Converged Network Adapter X710-DA2 [0006]",
    }})

    assert model == "Ethernet Controller X710 for 10GbE SFP+"


def test_the_chip_vendor_wins_over_the_card_brand():
    """"Dell" in ``subsystem_vendor`` is a real name here and is still ignored. The same Realtek
    silicon is soldered onto boards from a dozen brands; the component worth cataloguing is the
    Realtek."""
    vendor, _ = nic_identity({"pci_ids": REPORTED})

    assert vendor == "Realtek Semiconductor Co., Ltd."


def test_an_onboard_nic_is_named_by_its_controller_not_the_board():
    """Reported (run bdb2b8b8): an onboard Intel NIC came out with the *motherboard's* model. Its
    PCI subsystem is the board's, and pci.ids named that subsystem after a board - in fact a
    *different* board in the same family ("X11DPi-N") than the one actually fitted ("X11DPL-i"), so
    no amount of matching the machine's board model could have caught it. Preferring the chip's
    ``device`` - the controller, the same "Ethernet Connection X722 for 1GbE" lshw reports as the
    port's product - names it correctly with no board context at all."""
    nic = {"pci_ids": {
        "vendor": "Intel Corporation [8086]",
        "device": "Ethernet Connection X722 for 1GbE [37d1]",
        "subsystem_vendor": "Super Micro Computer Inc [15d9]",
        "subsystem_device": "X11DPi-N [1b4b]",
    }}
    assert nic_identity(nic) == ("Intel Corporation", "Ethernet Connection X722 for 1GbE")


def test_inspect_devices_shows_every_naming_source(submitter):
    """The ``inspect_devices`` command exists so a wrong name can be traced to its source: it prints
    all four lspci strings and marks which one became the model."""
    from io import StringIO

    from django.core.management import call_command

    run = _run(submitter, nics=[{
        "name": "eno1np0", "driver": "i40e", "pci": "0000:60:00.0",
        "pci_ids": {
            "vendor": "Intel Corporation [8086]",
            "device": "Ethernet Connection X722 for 1GbE [37d1]",
            "subsystem_vendor": "Super Micro Computer Inc [15d9]",
            "subsystem_device": "X11DPi-N [1b4b]",
        },
    }])
    out = StringIO()
    call_command("inspect_devices", str(run.uuid), stdout=out)
    text = out.getvalue()

    assert "X11DPi-N" in text, "the board subsystem is shown as a candidate source"
    assert "used as the model" in text, "and the winning field is marked"
    assert "catalogued as: Intel Corporation / Ethernet Connection X722 for 1GbE" in text


@pytest.mark.parametrize("value", [
    "Device [09a8]",        # what -nn prints
    "Device 09a8",          # what -mm alone prints
    "Unknown device 09a8",  # older lspci
    "device [09a8]",        # case is not guaranteed
    "", None,
])
def test_placeholders_read_as_no_name(value):
    assert pci_name(value) == ""


@pytest.mark.parametrize("value,expected", [
    ("Wi-Fi 6 AX200 [2723]", "Wi-Fi 6 AX200"),
    # A real product whose name merely starts with the word: it must survive.
    ("Device Server Adapter X999 [1234]", "Device Server Adapter X999"),
])
def test_real_names_survive(value, expected):
    assert pci_name(value) == expected


def test_a_nic_with_no_names_at_all_ties_nothing(submitter):
    """A machine without pciutils, a USB adapter, an SoC's built-in MAC."""
    run = _run(submitter, nics=[
        {"name": "usb0", "driver": "cdc_ether", "pci_ids": {}},
    ])

    assert ComponentKind.nic not in _kinds(run)


def test_a_nic_whose_only_names_are_placeholders_ties_nothing(submitter):
    run = _run(submitter, nics=[
        {"name": "enp2s0", "driver": "r8169", "pci_ids": {
            "vendor": "Device [10ec]", "device": "Device [8168]",
        }},
    ])

    assert ComponentKind.nic not in _kinds(run)


def test_no_component_is_ever_created_called_device(submitter, reviewer):
    """The outcome that matters, end to end on the reported data."""
    run = _run(submitter, nics=[
        {"name": "enp2s0", "driver": "r8169", "pci_ids": REPORTED},
    ])

    services.approve_run(ready(run), by=reviewer)

    assert not Component.objects.filter(name__iexact="Device").exists()
    nic = run.listing_components.get(kind=ComponentKind.nic.value)
    assert nic.name.startswith("RTL8111"), nic.name


# --- older bundles still work -----------------------------------------------------


def test_a_flattened_bundle_is_still_read(submitter):
    """Runs submitted between the two changes carry ``vendor`` and ``model`` instead of
    ``pci_ids``. They are read as a last resort so those runs keep working."""
    run = _run(submitter, nics=[
        {"name": "eno1", "vendor": "Intel Corporation",
         "model": "I350 Gigabit Network Connection", "driver": "igb"},
    ])

    assert ComponentKind.nic in _kinds(run)


def test_a_flattened_placeholder_is_still_rejected(submitter):
    """Exactly what run d4857592 holds: the choosing already happened, wrongly, and the answer was
    stored. Reading it through the same rule keeps a component called "Device" out of the catalog
    without anybody re-running that machine."""
    run = _run(submitter, nics=[
        {"name": "enp2s0", "vendor": "Realtek Semiconductor Co., Ltd.",
         "model": "Device", "driver": "r8169"},
    ])

    assert ComponentKind.nic not in _kinds(run)
