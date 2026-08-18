"""Raw model string -> catalog component matching (CPUs and GPUs)."""

import pytest

from lumina.hardware.models import Component, ComponentKind
from lumina.results.component_match import (
    find_or_create_component,
    match_component,
    normalize_cpu_model,
    normalize_gpu_model,
    strip_vendor_prefix,
)
from lumina.vendors.models import Vendor

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Intel(R) Xeon(R) Gold 6430", "Intel Xeon Gold 6430"),
        ("AMD Ryzen 9 7950X 16-Core Processor", "AMD Ryzen 9 7950X"),
        ("Intel(R) Core(TM) i7-8700K CPU @ 3.70GHz", "Intel Core i7-8700K"),
        ("AMD EPYC 9354 32-Core Processor", "AMD EPYC 9354"),
        ("Intel® Xeon® 6 Processors", "Intel Xeon 6"),
    ],
)
def test_normalize_cpu_model(raw, expected):
    assert normalize_cpu_model(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        # lspci names the die and brackets the product
        ("AD102 [GeForce RTX 4090]", "GeForce RTX 4090"),
        ("DG2 [Arc A380]", "Arc A380"),
        ("Navi 31 [Radeon RX 7900 XT/7900 XTX]", "Radeon RX 7900 XT/7900 XTX"),
        # nvidia-smi already gives the marketing name
        ("NVIDIA GeForce RTX 4090", "NVIDIA GeForce RTX 4090"),
        ("L40S", "L40S"),
    ],
)
def test_normalize_gpu_model(raw, expected):
    assert normalize_gpu_model(raw) == expected


def test_strip_vendor_prefix():
    assert strip_vendor_prefix("AMD Ryzen 9 7950X", "AMD") == "Ryzen 9 7950X"
    assert strip_vendor_prefix("Intel Xeon Gold 6430", "Intel") == "Xeon Gold 6430"
    assert strip_vendor_prefix("NVIDIA GeForce RTX 4090", "NVIDIA") == "GeForce RTX 4090"
    # no false stripping
    assert strip_vendor_prefix("Ryzen 9 7950X", "AMD") == "Ryzen 9 7950X"


def test_match_by_normalized_name():
    intel = Vendor.objects.get_or_create(name="Intel")[0]
    existing = Component.objects.create(
        vendor=intel, name="Xeon Gold 6430", kind=ComponentKind.cpu.value
    )
    assert match_component(intel, "Intel(R) Xeon(R) Gold 6430",
                           ComponentKind.cpu) == existing


def test_match_by_recorded_alias():
    amd = Vendor.objects.get_or_create(name="AMD")[0]
    existing = Component.objects.create(
        vendor=amd, name="Ryzen 9 7950X", kind=ComponentKind.cpu.value,
        attributes={"aliases": ["AMD Ryzen 9 7950X 16-Core Processor"]},
    )
    assert match_component(
        amd, "AMD Ryzen 9 7950X 16-Core Processor", ComponentKind.cpu
    ) == existing


def test_family_patterns_do_not_satisfy_a_model_lookup():
    """match_component answers "which specific part is this", so a family
    must never be returned - otherwise the per-model entry a leaderboard
    ranks could never be created once a family existed."""
    amd = Vendor.objects.get_or_create(name="AMD")[0]
    Component.objects.create(
        vendor=amd, name="EPYC 9004 Test Family", kind=ComponentKind.cpu.value,
        role="family", model_patterns=[r"EPYC 9[0-9]{2}4"],
    )
    assert match_component(
        amd, "AMD EPYC 9354 32-Core Processor", ComponentKind.cpu
    ) is None


def test_gpu_family_resolves_from_the_lspci_marketing_name():
    from lumina.results.component_match import family_for_model

    nvidia = Vendor.objects.get_or_create(name="NVIDIA")[0]
    family = Component.objects.create(
        vendor=nvidia, name="GeForce RTX 40 Series", kind=ComponentKind.gpu.value,
        role="family", model_patterns=[r"GeForce RTX 40[0-9]0"],
    )
    assert family_for_model(
        "AD102 [GeForce RTX 4090]", ComponentKind.gpu
    ) == family


def test_bad_pattern_never_breaks_family_resolution():
    """An unparseable pattern must be skipped, not raised."""
    from lumina.results.component_match import family_for_model

    amd = Vendor.objects.get_or_create(name="AMD")[0]
    Component.objects.create(
        vendor=amd, name="Broken Family", kind=ComponentKind.cpu.value,
        role="family", model_patterns=["EPYC ("],  # invalid regex
    )
    # resolution still succeeds, landing on the correctly seeded family
    assert family_for_model(
        "AMD EPYC 9354 32-Core Processor", ComponentKind.cpu
    ).name == "AMD EPYC 9004 Series"


def test_create_records_alias_and_strips_brand():
    """Creating a model entry is unaffected by a family that also matches."""
    amd = Vendor.objects.get_or_create(name="AMD")[0]
    component, created = find_or_create_component(
        amd, "AMD Ryzen 9 7950X 16-Core Processor", ComponentKind.cpu
    )
    assert created is True
    assert component.name == "Ryzen 9 7950X"
    assert component.attributes["aliases"] == ["AMD Ryzen 9 7950X 16-Core Processor"]
    # the raw string now matches the component it created
    again, created_again = find_or_create_component(
        amd, "AMD Ryzen 9 7950X 16-Core Processor", ComponentKind.cpu
    )
    assert again == component and created_again is False


def test_kinds_do_not_cross_match():
    """A GPU and CPU with the same name string stay separate entries."""
    amd = Vendor.objects.get_or_create(name="AMD")[0]
    cpu = Component.objects.create(
        vendor=amd, name="Instinct MI300A", kind=ComponentKind.cpu.value
    )
    gpu, created = find_or_create_component(
        amd, "Instinct MI300A", ComponentKind.gpu
    )
    assert created is True
    assert gpu != cpu
