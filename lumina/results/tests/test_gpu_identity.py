"""Naming a GPU is the server's job, not the collector's.

Asked for directly: "for GPUs we should decide server-side as well not in the collector", and then
the principle behind it: "the collector shouldn't really make any decisions. It's just collecting
and reporting. Keeping the raw data and decisions server-side in lumina helps us in case of issues
where we need to reprocess data in some way."

The GPU collector made three. It mapped the PCI vendor id through a five-entry table to a token
("nvidia"), it stripped the numeric ids off lspci's device name, and it let nvidia-smi's marketing
name overwrite that name. A fourth lived in the summary layer: an AMD APU's integrated GPU was
renamed from the CPU's brand string, because pci.ids has no marketing name for those dies.

All four are judgements. A bundle is written once and read for years, so a judgement in the reader
can be corrected for every bundle ever submitted, while one applied at collection time is frozen
into each of them. That is not theoretical - the NIC collector chose a name the same way and gave
two real NICs the model "Device", which no amount of server-side fixing could undo for the bundle
already submitted.

The cases below came from ``tests/test_parsers.py`` in the suite, which was their only home.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import Group, User

from lumina.hardware.models import ComponentKind
from lumina.releases.models import AlmaLinuxRelease
from lumina.results import ingest, services
from lumina.results.component_match import integrated_gpu_name
from lumina.results.models import TestRun
from lumina.results.pci_names import gpu_identity
from lumina.results.tests import factories as f

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def release():
    AlmaLinuxRelease.objects.get_or_create(major=9, defaults={"supported": True})


@pytest.fixture
def submitter():
    return User.objects.create_user("gpu-sub", password="pw")


@pytest.fixture
def reviewer():
    user = User.objects.create_user("gpu-rev", password="pw")
    group, _ = Group.objects.get_or_create(name="reviewer")
    user.groups.add(group)
    return user


def _amd(device, pci="c1:00.0", **extra):
    """A GPU record shaped the way the collector now reports one."""
    return {
        "pci": pci,
        "pci_ids": {
            "vendor": "Advanced Micro Devices, Inc. [AMD/ATI]",
            "device": device,
        },
        "driver": "amdgpu", "driver_version": None, "runtime": {}, "vbios": None,
        **extra,
    }


def _run(submitter, gpus, cpu_model=None):
    inventory = f.default_inventory()
    inventory["summary"]["gpus"] = gpus
    if cpu_model is not None:
        inventory["summary"]["cpus"] = [
            {**inventory["summary"]["cpus"][0], "model": cpu_model}
        ]
    run = ingest.ingest_bundle(
        submitter=submitter, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"], inventory=inventory,
            results=[f.validate_result("validate.cpu.functional")],
        ))),
    )
    return TestRun.objects.get(pk=run.pk)


def _gpus(run):
    return {gpu["pci"]: gpu["model"] for gpu in services.tieable_gpus(run)}


# --- which reported name is the product's ------------------------------------------


def test_the_vendor_comes_from_pci_ids():
    """No five-entry token table. pci.ids names every vendor there has ever been, and the alias
    table turns its spelling into the catalog's - which is what ``vendors.0002`` is for, since
    "Advanced Micro Devices, Inc. [AMD/ATI]" normalizes to nothing like "AMD"."""
    vendor, _ = gpu_identity(_amd("Navi 33 [Radeon RX 7600] [7480]"))

    assert vendor == "Advanced Micro Devices, Inc. [AMD/ATI]"


def test_nvidia_smi_beats_lspci():
    """It gives the marketing name where lspci gives the die. Reported as its own field now, so
    both survive in the bundle and the preference is the reader's."""
    _, model = gpu_identity({
        "pci_ids": {"vendor": "NVIDIA Corporation [10de]",
                    "device": "AD102 [GeForce RTX 4090] [2684]"},
        "smi_name": "NVIDIA GeForce RTX 4090",
    })

    assert model == "NVIDIA GeForce RTX 4090"


def test_the_board_beats_the_die():
    """A partner's board name is what somebody bought."""
    _, model = gpu_identity({
        "pci_ids": {"vendor": "NVIDIA Corporation [10de]",
                    "device": "AD104 [GeForce RTX 4070] [2786]",
                    "subsystem_device": "TUF Gaming RTX 4070 OC [88e2]"},
    })

    assert model == "TUF Gaming RTX 4070 OC"


def test_a_placeholder_subsystem_falls_through():
    """The bug that started all of this, on a GPU: pci.ids has no name for most board
    subsystems and lspci writes "Device [1234]" there."""
    _, model = gpu_identity({
        "pci_ids": {"vendor": "Intel Corporation [8086]",
                    "device": "CometLake-S GT2 [UHD Graphics 630] [9bc5]",
                    "subsystem_device": "Device [09a8]"},
    })

    assert model == "CometLake-S GT2 [UHD Graphics 630]"


def test_an_older_bundle_still_works():
    """Runs submitted while the collector flattened these carry a token and a stripped name."""
    vendor, model = gpu_identity({"vendor": "nvidia", "model": "L40S"})

    assert (vendor, model) == ("NVIDIA", "L40S")


# --- an AMD APU's integrated GPU ---------------------------------------------------


@pytest.mark.parametrize("cpu_model,expected", [
    ("AMD Ryzen 7 PRO 7840U w/ Radeon 780M Graphics", "Radeon 780M"),
    ("AMD Ryzen AI 9 HX 370 w/ Radeon 890M", "Radeon 890M"),
    ("AMD Ryzen 5 2400G with Radeon Vega 11 Graphics", "Radeon Vega 11"),
    ("AMD Ryzen 5 3400G with Radeon Vega Graphics", "Radeon Vega"),
    ("AMD Ryzen Embedded V1605B with Radeon Vega Gfx", "Radeon Vega"),
    # No marketing model exists for these, so the generic string is the truth rather than a
    # bare "Radeon".
    ("AMD Ryzen 7 5800H with Radeon Graphics", "Radeon Graphics"),
    # Nothing to extract.
    ("AMD Ryzen 5 7600X", ""),
    ("AMD EPYC 9654 96-Core Processor", ""),
    ("Intel(R) Xeon(R) Gold 6430", ""),
])
def test_the_name_carried_in_a_cpu_brand_string(cpu_model, expected):
    assert integrated_gpu_name(cpu_model) == expected


def test_a_die_codename_is_replaced_by_the_product_name(submitter):
    """Regression, run 9aac4289: a ThinkPad's iGPU was catalogued as "Phoenix1", the amdgpu die
    codename, because lspci never reports a product name for integrated graphics."""
    run = _run(
        submitter, [_amd("Phoenix1 [15bf]")],
        cpu_model="AMD Ryzen 7 PRO 7840U w/ Radeon 780M Graphics",
    )

    gpu = services.tieable_gpus(run)[0]

    assert gpu["model"] == "Radeon 780M"
    assert gpu["asic"] == "Phoenix1", "the silicon generation is not lost"


def test_a_named_amd_card_keeps_its_own_name(submitter):
    """pci.ids brackets discrete cards, so they need no help and must not be overwritten by
    whatever the CPU happens to have on package."""
    run = _run(
        submitter, [_amd("Navi 33 [Radeon RX 7600] [7480]")],
        cpu_model="AMD Ryzen 7 PRO 7840U w/ Radeon 780M Graphics",
    )

    assert _gpus(run) == {"c1:00.0": "Navi 33 [Radeon RX 7600]"}


def test_an_apu_beside_a_discrete_card_names_only_the_apu(submitter):
    run = _run(
        submitter,
        [_amd("Navi 33 [Radeon RX 7600M XT] [7480]", pci="03:00.0"),
         _amd("Phoenix1 [15bf]", pci="c1:00.0")],
        cpu_model="AMD Ryzen 9 7940HS w/ Radeon 780M Graphics",
    )

    assert _gpus(run) == {
        "03:00.0": "Navi 33 [Radeon RX 7600M XT]",
        "c1:00.0": "Radeon 780M",
    }


def test_two_unidentifiable_amd_gpus_are_left_alone(submitter):
    """With no way to tell which one the CPU string describes, guessing would put a wrong product
    name on real certification evidence."""
    run = _run(
        submitter,
        [_amd("Phoenix1 [15bf]", pci="c1:00.0"), _amd("Strix [150e]", pci="c2:00.0")],
        cpu_model="AMD Ryzen 9 7940HS w/ Radeon 780M Graphics",
    )

    assert _gpus(run) == {"c1:00.0": "Phoenix1", "c2:00.0": "Strix"}


def test_non_amd_gpus_are_untouched(submitter):
    run = _run(
        submitter,
        [{"pci": "01:00.0",
          "pci_ids": {"vendor": "NVIDIA Corporation [10de]",
                      "device": "AD107M [GeForce RTX 4060] [28e0]"},
          "driver": "nvidia", "driver_version": None, "runtime": {}, "vbios": None}],
        cpu_model="AMD Ryzen 7 PRO 7840U w/ Radeon 780M Graphics",
    )

    assert _gpus(run) == {"01:00.0": "AD107M [GeForce RTX 4060]"}


# --- and it reaches the catalog ----------------------------------------------------


def test_the_resolved_name_is_what_makes_the_family_match(submitter, reviewer):
    """End to end, and this is what the naming is *for*.

    "Radeon 780M" matches the curated "AMD Radeon 700M Series (integrated)" family, so the run
    certifies the family the way GPU certification is meant to work. The die codename lspci
    reported matches nothing, so with the collector's raw string alone the run would have minted a
    component called "Phoenix1" instead - which is exactly what run 9aac4289 did.

    Also covers the alias without which AMD's pci.ids spelling resolves to no vendor at all, and
    ``_vendor_for`` mints one called "Advanced Micro Devices, Inc. [AMD/ATI]".
    """
    from lumina.hardware.models import Component, ComponentRole
    from lumina.results.tests.helpers import release as ready
    from lumina.vendors.models import Vendor, VendorAlias
    from lumina.vendors.pci_aliases import ensure

    ensure(Vendor, VendorAlias)
    run = _run(
        submitter, [_amd("Phoenix1 [15bf]")],
        cpu_model="AMD Ryzen 7 PRO 7840U w/ Radeon 780M Graphics",
    )

    services.approve_run(ready(run), by=reviewer)

    run.refresh_from_db()
    gpu = run.listing_components.get(kind=ComponentKind.gpu.value)
    assert gpu.role == ComponentRole.FAMILY, "certification applies to the family"
    assert "700M" in gpu.name, gpu.name
    assert gpu.vendor.name == "AMD", "the pci.ids spelling resolved through the alias table"
    assert not Component.objects.filter(name="Phoenix1").exists()


def test_without_the_cpu_context_there_is_no_family_to_match(submitter, reviewer):
    """The contrast, which is the whole reason this rule exists: the codename matches nothing."""
    from lumina.hardware.models import Component
    from lumina.results.tests.helpers import release as ready
    from lumina.vendors.models import Vendor, VendorAlias
    from lumina.vendors.pci_aliases import ensure

    ensure(Vendor, VendorAlias)
    run = _run(submitter, [_amd("Phoenix1 [15bf]")], cpu_model="AMD Ryzen 5 7600X")

    services.approve_run(ready(run), by=reviewer)

    assert Component.objects.filter(name="Phoenix1").exists()


# --- which card the run is filed under -------------------------------------------------


def test_a_discrete_card_beats_the_management_adapter():
    """Nearly every server has a Matrox or ASPEED adapter driving a VGA console nobody looks at,
    and lspci often lists it first.

    ``_primary_gpu`` compared ``gpu["vendor"]`` against three tokens, and there is no such key: the
    collector stopped flattening it so a naming rule that turned out wrong could be corrected for
    bundles already submitted. The discrete list was therefore always empty and this fell through
    to "whatever lspci listed first", which is the adapter.
    """
    from lumina.results.inventory_extract import _primary_gpu

    matrox = {
        "pci": "07:00.0",
        "pci_ids": {"vendor": "Matrox Electronics Systems Ltd. [102b]",
                    "device": "Integrated Matrox G200eW3 Graphics Controller [0536]"},
        "driver": "mgag200",
    }
    l40s = {
        "pci": "41:00.0",
        "pci_ids": {"vendor": "NVIDIA Corporation [10de]",
                    "device": "AD102GL [L40S] [26b9]"},
        "driver": "nvidia", "smi_name": "NVIDIA L40S", "runtime": {"cuda": "12.4"},
    }

    assert _primary_gpu([matrox, l40s]) is l40s


def test_the_extracted_gpu_column_names_the_accelerator():
    """The column the run pages and the leaderboards display."""
    from lumina.results.inventory_extract import extract

    inventory = {"summary": {"gpus": [
        {"pci_ids": {"vendor": "ASPEED Technology, Inc. [1a03]",
                     "device": "ASPEED Graphics Family [2000]"}, "driver": "ast"},
        {"pci_ids": {"vendor": "NVIDIA Corporation [10de]",
                     "device": "AD102GL [L40S] [26b9]"},
         "driver": "nvidia", "smi_name": "NVIDIA L40S"},
    ]}}

    assert extract(inventory)["gpu_model"] == "NVIDIA L40S"


def test_an_adapter_is_still_reported_when_it_is_all_there_is():
    """A machine with only a management adapter has one GPU, and blanking the column would say the
    machine has none."""
    from lumina.results.inventory_extract import _primary_gpu

    adapter = {
        "pci_ids": {"vendor": "ASPEED Technology, Inc. [1a03]",
                    "device": "ASPEED Graphics Family [2000]"},
        "driver": "ast",
    }

    assert _primary_gpu([adapter]) is adapter
