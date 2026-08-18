"""The seeded GPU families, checked against real reported model strings.

Both forms the tools produce are covered: lspci's "die [marketing name]" and
nvidia-smi's plain marketing name. The near-miss cases matter most here,
because NVIDIA reuses numbers across product lines - "RTX 4000 Ada" is a
workstation card, not a GeForce RTX 40-series part.
"""
from __future__ import annotations

import pytest

from lumina.hardware.models import Component, ComponentKind, ComponentRole
from lumina.results.component_match import family_for_model

pytestmark = pytest.mark.django_db


def resolve(model: str) -> str | None:
    family = family_for_model(model, ComponentKind.gpu)
    return family.name if family else None


# --- NVIDIA GeForce -----------------------------------------------------------


@pytest.mark.parametrize(
    "model,expected",
    [
        # lspci form: die codename plus the bracketed product
        ("TU116 [GeForce GTX 1660 SUPER]", "NVIDIA GeForce GTX 16 Series"),
        ("TU117 [GeForce GTX 1650]", "NVIDIA GeForce GTX 16 Series"),
        ("TU106 [GeForce RTX 2060]", "NVIDIA GeForce RTX 20 Series"),
        ("TU102 [GeForce RTX 2080 Ti]", "NVIDIA GeForce RTX 20 Series"),
        ("GA106 [GeForce RTX 3060]", "NVIDIA GeForce RTX 30 Series"),
        ("GA102 [GeForce RTX 3090 Ti]", "NVIDIA GeForce RTX 30 Series"),
        ("AD102 [GeForce RTX 4090]", "NVIDIA GeForce RTX 40 Series"),
        ("AD104 [GeForce RTX 4070 Ti SUPER]", "NVIDIA GeForce RTX 40 Series"),
        ("GB202 [GeForce RTX 5090]", "NVIDIA GeForce RTX 50 Series"),
        # nvidia-smi form: the marketing name directly
        ("NVIDIA GeForce RTX 4080 SUPER", "NVIDIA GeForce RTX 40 Series"),
        ("NVIDIA GeForce RTX 3050", "NVIDIA GeForce RTX 30 Series"),
        # laptop parts keep desktop numbering and belong to the same family
        ("NVIDIA GeForce RTX 4090 Laptop GPU", "NVIDIA GeForce RTX 40 Series"),
        ("AD107M [GeForce RTX 4050 Max-Q / Mobile]",
         "NVIDIA GeForce RTX 40 Series"),
    ],
)
def test_nvidia_geforce_generations(model, expected):
    assert resolve(model) == expected


@pytest.mark.parametrize(
    "model",
    [
        # workstation cards share the numbering but are a different line
        "NVIDIA RTX 4000 Ada Generation",
        "AD104GL [RTX 4000 Ada Generation]",
        "GA104GL [RTX A4000]",
        "NVIDIA RTX 6000 Ada Generation",
        # data-center parts, not yet curated
        "AD102GL [L40S]",
        "GA100 [A100 SXM4 80GB]",
        "GH100 [H100 SXM5 80GB]",
        # older consumer generations, not yet curated
        "GP104 [GeForce GTX 1080]",
    ],
)
def test_out_of_scope_nvidia_parts_match_nothing(model):
    """They surface under their own model name rather than being folded into
    a consumer family they do not belong to."""
    assert resolve(model) is None


def test_workstation_x000_is_not_swallowed_by_the_geforce_pattern():
    """The reason the third digit is pinned to 5-9."""
    assert resolve("AD104GL [RTX 4000 Ada Generation]") is None
    assert resolve("AD102 [GeForce RTX 4090]") == "NVIDIA GeForce RTX 40 Series"


# --- Intel Arc ----------------------------------------------------------------


@pytest.mark.parametrize(
    "model,expected",
    [
        ("DG2 [Arc A380]", "Intel Arc A-Series (Alchemist)"),
        ("DG2 [Arc A770]", "Intel Arc A-Series (Alchemist)"),
        ("DG2 [Arc A310]", "Intel Arc A-Series (Alchemist)"),
        ("DG2M [Arc A730M]", "Intel Arc A-Series (Alchemist)"),
        ("Intel Arc A750 Graphics", "Intel Arc A-Series (Alchemist)"),
        ("BMG G21 [Arc B580]", "Intel Arc B-Series (Battlemage)"),
        ("BMG G21 [Arc B570]", "Intel Arc B-Series (Battlemage)"),
    ],
)
def test_intel_arc_generations(model, expected):
    assert resolve(model) == expected


def test_integrated_intel_graphics_match_nothing():
    """iGPUs are not Arc discrete parts and have no family yet."""
    assert resolve("AlderLake-S GT1") is None
    assert resolve("UHD Graphics 630") is None
    assert resolve("Raptor Lake-S GT1 [UHD Graphics 770]") is None


# --- the seeded set -----------------------------------------------------------


def test_gpu_families_are_seeded_unpublished():
    families = Component.objects.filter(
        kind=ComponentKind.gpu.value, role=ComponentRole.FAMILY
    )
    # Counted per vendor rather than in total: a bare total breaks on every
    # new seed migration while catching nothing the rest of this does not.
    for vendor in ("NVIDIA", "Intel", "AMD"):
        assert families.filter(vendor__name=vendor).exists(), vendor
    assert not families.filter(published=True).exists()
    assert not families.filter(model_patterns=[]).exists()


def test_no_two_gpu_families_match_the_same_model():
    """Overlapping patterns would make resolution order-dependent."""
    from lumina.results.component_match import matches_family

    samples = [
        "AD102 [GeForce RTX 4090]", "GA106 [GeForce RTX 3060]",
        "TU106 [GeForce RTX 2060]", "TU116 [GeForce GTX 1660 SUPER]",
        "GB202 [GeForce RTX 5090]", "DG2 [Arc A380]", "BMG G21 [Arc B580]",
        # AMD reuses "RX 5" for a four-digit RDNA part and a three-digit
        # Polaris one, and "Vega" for both discrete cards and iGPUs.
        "Navi 14 [Radeon RX 5500 XT]", "Baffin [Radeon RX 550]",
        "Ellesmere [Radeon RX 480]", "Navi 48 [Radeon RX 9070 XT]",
        "Vega 10 XT [Radeon RX Vega 64]", "Radeon RX Vega 11",
        "Radeon Vega 8", "Radeon 680M", "Radeon 780M", "Radeon 890M",
        "Radeon Pro W7900", "Radeon Pro WX 7100",
        "Instinct MI100", "Instinct MI250X", "Instinct MI300X",
    ]
    families = Component.objects.filter(
        role=ComponentRole.FAMILY, kind=ComponentKind.gpu.value
    )
    for model in samples:
        hits = [f.name for f in families if matches_family(f, model)]
        assert len(hits) == 1, f"{model} matched {hits}"


def test_gpu_and_cpu_families_do_not_cross_kinds():
    """A GPU string must not resolve against a CPU family, or vice versa."""
    assert family_for_model("AD102 [GeForce RTX 4090]", ComponentKind.cpu) is None
    assert family_for_model(
        "AMD EPYC 9354 32-Core Processor", ComponentKind.gpu
    ) is None


# --- AMD ----------------------------------------------------------------------

# AMD reuses "RX 5" for both a four-digit RDNA part and a three-digit Polaris
# one, and "Vega" for both discrete cards and iGPUs, so the near-miss pairs
# here are the whole point.
@pytest.mark.parametrize(
    "model,expected",
    [
        # discrete RDNA, four digits
        ("Navi 48 [Radeon RX 9070 XT]", "AMD Radeon RX 9000 Series (RDNA 4)"),
        ("Navi 31 [Radeon RX 7900 XTX]", "AMD Radeon RX 7000 Series (RDNA 3)"),
        ("Navi 33 [Radeon RX 7600]", "AMD Radeon RX 7000 Series (RDNA 3)"),
        ("Navi 21 [Radeon RX 6950 XT]", "AMD Radeon RX 6000 Series (RDNA 2)"),
        ("Navi 24 [Radeon RX 6400]", "AMD Radeon RX 6000 Series (RDNA 2)"),
        ("Navi 10 [Radeon RX 5700 XT]", "AMD Radeon RX 5000 Series (RDNA)"),
        # the collision that needs the lookahead: RX 5500 is RDNA, RX 550 is
        # Polaris, and both begin "RX 5"
        ("Navi 14 [Radeon RX 5500 XT]", "AMD Radeon RX 5000 Series (RDNA)"),
        ("Baffin [Radeon RX 550]", "AMD Radeon RX 500 Series (Polaris)"),
        ("Ellesmere [Radeon RX 580]", "AMD Radeon RX 500 Series (Polaris)"),
        ("Ellesmere [Radeon RX 480]", "AMD Radeon RX 400 Series (Polaris)"),
        # discrete Vega is 56/64; everything lower is an iGPU
        ("Vega 10 XT [Radeon RX Vega 64]", "AMD Radeon RX Vega Series"),
        ("Vega 10 XL [Radeon RX Vega 56]", "AMD Radeon RX Vega Series"),
        # integrated Vega, including the 2400G which reports itself with "RX"
        ("Radeon RX Vega 11", "AMD Radeon Vega Graphics (integrated)"),
        ("Radeon Vega 8", "AMD Radeon Vega Graphics (integrated)"),
        ("Radeon Vega 3", "AMD Radeon Vega Graphics (integrated)"),
        # integrated RDNA: three digits plus M, resolved from the CPU brand
        # string by the suite because lspci only reports the die codename
        ("Radeon 680M", "AMD Radeon 600M Series (integrated)"),
        ("Radeon 780M", "AMD Radeon 700M Series (integrated)"),
        ("Radeon 890M", "AMD Radeon 800M Series (integrated)"),
        # workstation and data center
        ("Radeon Pro W7900", "AMD Radeon Pro W Series"),
        ("Radeon Pro WX 7100", "AMD Radeon Pro WX Series"),
        ("Instinct MI100", "AMD Instinct MI100"),
        ("Instinct MI250X", "AMD Instinct MI200 Series"),
        ("Instinct MI300X", "AMD Instinct MI300 Series"),
        # No number means no model information, so inventing a generation
        # would be a lie. These stay unfamilied.
        ("Radeon Graphics", None),
        ("Radeon Vega", None),
        # A bare die codename never reaches a family: the suite resolves the
        # product name before submitting, and guessing here would be wrong.
        ("Phoenix1", None),
        ("Renoir", None),
    ],
)
def test_amd_gpu_families(model, expected):
    assert resolve(model) == expected
