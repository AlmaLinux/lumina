"""Raw model string -> catalog component matching (CPUs and GPUs)."""

import pytest

from lumina.hardware.models import Component, ComponentKind
from lumina.results.component_match import (
    find_or_create_component,
    gpu_name_identifies,
    gpu_name_status,
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


# --- why a part is called what it is -----------------------------------------------
#
# A name a rule produced, a name the built-in behaviour produced, and a name straight off the
# machine were indistinguishable once resolved. They are three different things to a reviewer
# deciding whether to say anything.

INTEL_IGPU = {"driver": "i915", "pci_ids": {"vendor": "Intel Corporation [8086]",
                                            "device": "Arrow Lake-S [Intel Graphics] [7d67]"}}
AMD_APU = {"driver": "amdgpu",
           "pci_ids": {"vendor": "Advanced Micro Devices, Inc. [AMD/ATI] [1002]",
                       "device": "Phoenix1 [15bf]"}}
NVIDIA = {"driver": "nvidia", "pci_ids": {"vendor": "NVIDIA Corporation [10de]",
                                          "device": "AD104 [GeForce RTX 4070] [2786]"}}


def test_a_rule_is_reported_as_the_reason_for_the_name():
    from lumina.hardware.models import ComponentNamingRule

    rule = ComponentNamingRule.objects.create(
        kind="gpu", priority=1, build="Arc-era integrated",
        conditions=[{"field": "pci.vendor_id", "op": "eq", "value": "8086"}])

    status = gpu_name_status(INTEL_IGPU, "Intel(R) Core(TM) Ultra 9 275HX")

    assert status["name"] == "Arc-era integrated"
    assert status["rule"] == rule
    assert status["changed"] is True


def test_with_no_rule_matching_the_machine_s_own_name_stands():
    """There is no second naming behaviour under the table any more. Qualifying a generic name
    with its CPU and swapping an AMD die codename for the product the brand string advertises were
    both in code, beneath the rules, which meant the one AMD phrasing somebody had anticipated was
    the only one that could work. Both are seeded rules now, and turning them off means exactly
    what it says."""
    from lumina.hardware.models import ComponentNamingRule

    ComponentNamingRule.objects.update(enabled=False)

    status = gpu_name_status(INTEL_IGPU, "Intel(R) Core(TM) Ultra 9 275HX")

    assert status["name"] == "Intel Graphics"
    assert status["rule"] is None
    assert status["changed"] is False


def test_a_name_nothing_touched_is_reported_unchanged():
    status = gpu_name_status(NVIDIA)

    assert status == {"reported": "GeForce RTX 4070", "name": "GeForce RTX 4070",
                      "rule": None, "changed": False, "unidentified": False}


def test_an_amd_die_codename_counts_as_identifying_nothing():
    """The case this whole area started from: a census counted machines under "HawkPoint1"."""
    status = gpu_name_status(AMD_APU, "AMD Ryzen 7 7840U")

    assert status["name"] == "Phoenix1"
    assert status["unidentified"] is True


def test_a_generic_name_identifies_nothing():
    assert gpu_name_identifies("Intel Corporation", "Intel Graphics") is False


def test_a_radeon_product_name_identifies_a_part():
    """The AMD half of the test only fires on names that are not products, or every AMD card on
    the site would be flagged. Spelled as pci.ids spells it, since that is the string this reads
    and it is the bracketed half that carries "AMD"."""
    assert gpu_name_identifies(
        "Advanced Micro Devices, Inc. [AMD/ATI]", "Radeon RX 7600") is True


def test_an_amd_codename_is_flagged_under_the_same_spelling():
    """The pair to the test above: same vendor string, and only the name differs."""
    assert gpu_name_identifies("Advanced Micro Devices, Inc. [AMD/ATI]", "Phoenix1") is False


def test_a_codename_from_another_vendor_is_left_alone():
    """The AMD test is AMD's: applying it to NVIDIA would flag every GeForce, since none of them
    say Radeon."""
    assert gpu_name_identifies("NVIDIA Corporation", "GeForce RTX 4070") is True
