"""Matching raw CPU/GPU model strings to catalog components.

The tools report marketing soup: "Intel(R) Xeon(R) Gold 6430",
"AMD Ryzen 9 7950X 16-Core Processor", "AD102 [GeForce RTX 4090]",
"Navi 31 [Radeon RX 7900 XT/7900 XTX]". The catalog wants stable entries,
often at *family* granularity ("AMD EPYC(TM) 9004 Series", "GeForce RTX 40
Series"). This module bridges the two:

1. normalize the raw string (kind-specific: CPUs drop (R)/(TM) marks, "CPU",
   clock suffix, "N-Core Processor"; GPUs prefer the bracketed marketing
   name lspci provides over the die codename),
2. try to match an existing component of the same vendor and kind - by
   normalized name, by recorded aliases, then by admin-curated regexes in
   ``Component.model_patterns`` (how "EPYC 9354" ties to the 9004 series, or
   "GeForce RTX 4090" to the RTX 40 family: model->family rules are domain
   knowledge, not string similarity, so humans own the patterns),
3. otherwise create a new component named by the cleaned model with the
   vendor brand stripped, keeping the raw string in
   ``attributes["aliases"]`` so the next report of the same silicon matches
   without any of this work.
"""

from __future__ import annotations

import re

from lumina.hardware.models import Component, ComponentKind

_MARKS_RE = re.compile(r"\((?:R|TM|C)\)|[®™©]", re.I)
_CLOCK_RE = re.compile(r"@\s*[\d.]+\s*[GM]Hz", re.I)
_NCORE_RE = re.compile(r"\b\d+-Cores?\s+Processor\b", re.I)
_CPU_WORD_RE = re.compile(r"\bCPU\b", re.I)
_PROC_WORD_RE = re.compile(r"\bProcessors?\b", re.I)
_ZERO_RE = re.compile(r"\b0\b(?:\s*@.*)?$")  # lscpu sometimes appends " 0"
_BRACKET_RE = re.compile(r"\[([^\]]+)\]")


def normalize_cpu_model(model: str) -> str:
    """Reduce a raw CPU string to its distinctive parts.

    "Intel(R) Xeon(R) Gold 6430" -> "Intel Xeon Gold 6430"
    "AMD Ryzen 9 7950X 16-Core Processor" -> "AMD Ryzen 9 7950X"
    "Intel(R) Core(TM) i7-8700K CPU @ 3.70GHz" -> "Intel Core i7-8700K"
    """
    s = model or ""
    for pattern in (_MARKS_RE, _CLOCK_RE, _NCORE_RE, _CPU_WORD_RE, _PROC_WORD_RE,
                    _ZERO_RE):
        s = pattern.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip(" -")


def strip_vendor_prefix(cleaned: str, vendor_name: str) -> str:
    """Drop a leading vendor brand from a cleaned model string.

    Component display is "<vendor> <name>", so keeping "AMD" inside the name
    would render as "AMD AMD Ryzen 9 7950X".
    """
    tokens = cleaned.split()
    brand_tokens = (vendor_name or "").split()
    if not brand_tokens:
        # No vendor resolved, so there is no prefix to strip. Reachable since
        # ``catalog_name`` reports on a string whose brand may match nothing in the
        # catalog yet; every other caller passes a saved Vendor and never hits it.
        return cleaned
    while tokens and brand_tokens and tokens[0].lower() == brand_tokens[0].lower():
        tokens = tokens[1:]
        brand_tokens = brand_tokens[1:]
    # also handle the plain one-word brand ("Intel Corporation" vs "Intel")
    if tokens and tokens[0].lower() == vendor_name.split()[0].lower():
        tokens = tokens[1:]
    return " ".join(tokens) or cleaned


# The trailing description pci.ids appends after the part number, and only there.
#
# These strings are "<part> <what it is>": "RTL8111/8168/8411 PCI Express Gigabit Ethernet
# Controller", "I350 Gigabit Network Connection". Stripping the tail leaves the part; stripping
# these words *anywhere* was the first attempt and it mangled the names that lead with them -
# "Ethernet Converged Network Adapter X710-DA2" came out as "Converged X710-DA2". Leading
# boilerplate is left alone, which reads as slightly long rather than as garbled.
_NIC_TAIL_RE = re.compile(
    r"(?:\s+(?:PCI\s+Express|PCIe|Gigabit|Ethernet|Network|Fast|"
    r"Controller|Adapter|Connection|Interface))+$",
    re.I,
)


# An AMD APU's brand string carries its iGPU's product name: "AMD Ryzen 7 PRO 7840U w/ Radeon
# 780M Graphics". pci.ids has no marketing name for those dies, so lspci reports only a codename
# ("Phoenix1") and the part is unsearchable without this.
_IGPU_IN_CPU_RE = re.compile(r"\bw(?:/|ith)\s+(Radeon\b.*)$", re.I)
_IGPU_SUFFIX_RE = re.compile(r"\s+(?:Graphics|Gfx)$", re.I)
# A GPU string carrying any of these is already a product name rather than a die codename, so it
# is left alone: "Navi 33 [Radeon RX 7600]".
GPU_MARKETING_RE = re.compile(r"\b(Radeon|RX|Vega|Instinct|FirePro)\b", re.I)


def integrated_gpu_name(cpu_model: str) -> str:
    """The iGPU product name carried in an AMD CPU brand string, or "".

    Moved here from the collector, where it rewrote the GPU's model before the report was even
    written. Same rule, applied by the reader - so a bundle keeps the CPU string and the die
    codename it actually observed, and this can be corrected for every bundle ever submitted.
    """
    match = _IGPU_IN_CPU_RE.search(cpu_model or "")
    if not match:
        return ""
    name = " ".join(match.group(1).split())
    trimmed = _IGPU_SUFFIX_RE.sub("", name)
    # "Radeon 780M Graphics" -> "Radeon 780M", but "Radeon Graphics" keeps its suffix: stripped to
    # a bare "Radeon" it is not a name at all. Those parts genuinely have no marketing model, so
    # the generic string is the truth.
    return name if trimmed.lower() in ("radeon", "radeon rx") else trimmed


def normalize_gpu_model(model: str) -> str:
    """Reduce a raw GPU string to its marketing identity.

    lspci names the die and brackets the product: "AD102 [GeForce RTX 4090]"
    -> "GeForce RTX 4090". nvidia-smi already gives the marketing name
    ("NVIDIA GeForce RTX 4090"), which passes through minus trademark marks.
    """
    s = model or ""
    bracket = _BRACKET_RE.search(s)
    if bracket:
        s = bracket.group(1)
    s = _MARKS_RE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip(" -")


# Software rasterizers a benchmark can enumerate and mislabel as a GPU: lavapipe (Vulkan) and
# rusticl-on-llvmpipe (OpenCL), both of which report the device name "llvmpipe". They run on the
# CPU and are not graphics hardware. The suite (almacert.gpudev) now excludes CPU devices at the
# source - including CPU OpenCL runtimes (pocl, Intel's) that report a bare CPU brand name, which
# it recognises by device *type*. This name check is the backstop for a bundle from an older suite;
# it catches the rasterizers, which are named for what they are. A brand-named CPU runtime in such
# an old bundle carries no type and cannot be told from a GPU by name, so it relies on the source
# fix. Kept in step with almacert.gpudev.SOFTWARE_DEVICE_MARKERS.
_SOFTWARE_GPU_MARKERS = ("llvmpipe", "swrast", "softpipe", "lavapipe", "swiftshader")


def is_software_gpu(model: str) -> bool:
    """Whether a GPU device string names a software rasterizer (a CPU implementation) not hardware.

    Name-based, which reliably catches the rasterizers (llvmpipe and kin). A CPU OpenCL runtime that
    reports a plain CPU brand string has no marker and is handled at the source by device type
    instead; see almacert.gpudev.
    """
    lowered = (model or "").lower()
    return any(marker in lowered for marker in _SOFTWARE_GPU_MARKERS)


def normalize_board_model(model: str) -> str:
    """Motherboard strings are comparatively clean ("B650M PG Riptide",
    "0M83RH", "MPG X670E CARBON WIFI (MS-7D70)"); just strip trademark
    marks and collapse whitespace. The parenthetical internal model some
    vendors append is part of the identity and stays."""
    s = _MARKS_RE.sub(" ", model or "")
    return re.sub(r"\s+", " ", s).strip(" -")


def normalize_nic_model(model: str) -> str:
    """Reduce a raw NIC string to the part somebody would search for.

    pci.ids names a network device the way its datasheet does, which is long and carries the bus
    and the interface as prose: "RTL8111/8168/8411 PCI Express Gigabit Ethernet Controller",
    "BCM57414 NetXtreme-E 10Gb/25Gb RDMA Ethernet Controller", "Ethernet Controller X710 for
    10GbE SFP+". The part number is what identifies it; the rest is a description of what the
    part is, repeated on every entry from that vendor.

    Only the *trailing* description goes, and brackets are read the way GPUs read them - lspci
    uses the same "chip [product]" shape for both. Deliberately conservative: dropping too much
    would merge two genuinely different cards, and a near-duplicate is easy to correct through
    the per-component override where a wrong merge has to be untangled.
    """
    s = model or ""
    bracket = _BRACKET_RE.search(s)
    if bracket:
        s = bracket.group(1)
    s = _MARKS_RE.sub(" ", s)
    s = _NIC_TAIL_RE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip(" -")


NORMALIZERS = {
    ComponentKind.cpu: normalize_cpu_model,
    ComponentKind.gpu: normalize_gpu_model,
    ComponentKind.motherboard: normalize_board_model,
    ComponentKind.nic: normalize_nic_model,
}


def _comparable(value: str, kind: ComponentKind = ComponentKind.cpu) -> str:
    return NORMALIZERS[kind](value).casefold()


def families(vendor=None, kind: ComponentKind = ComponentKind.cpu) -> list:
    """Family-role components for a kind (optionally one vendor).

    Role is declared, not inferred: a family without patterns matches
    nothing yet, which the admin surfaces as a warning rather than silently
    treating it as a model.
    """
    from lumina.hardware.models import ComponentRole

    qs = Component.objects.filter(
        kind=kind.value, role=ComponentRole.FAMILY
    ).exclude(model_patterns=[])
    if vendor is not None:
        qs = qs.filter(vendor=vendor)
    return list(qs.select_related("vendor"))


def matches_family(family, raw_model: str) -> bool:
    """Whether one family's patterns match a reported model string."""
    kind = ComponentKind(family.kind)
    normalized = NORMALIZERS[kind](raw_model or "")
    for pattern in family.model_patterns or []:
        try:
            if re.search(pattern, raw_model or "", re.I) or re.search(
                pattern, normalized, re.I
            ):
                return True
        except re.error:
            continue
    return False


def family_for_model(raw_model: str, kind: ComponentKind, vendor=None):
    """The family a raw model string rolls up to, or None.

    Pattern-only: a family is defined by what it matches, never by name
    similarity. Resolved on read rather than stored, so adding a pattern in
    the admin immediately reclassifies existing results without a backfill.
    """
    if not (raw_model or "").strip():
        return None
    for family in families(vendor=vendor, kind=kind):
        if matches_family(family, raw_model):
            return family
    return None


def group_models_by_family(
    raw_models: list, kind: ComponentKind
) -> dict[str, list]:
    """Map each family name (or the raw model, unfamilied) to its models.

    One pass over the curated families for the whole set, so a leaderboard
    resolving hundreds of model strings stays cheap.
    """
    resolved: dict[str, list] = {}
    cache: dict[str, str] = {}
    for raw in raw_models:
        if raw in cache:
            resolved.setdefault(cache[raw], []).append(raw)
            continue
        family = family_for_model(raw, kind)
        key = family.name if family else raw
        cache[raw] = key
        resolved.setdefault(key, []).append(raw)
    return resolved


def match_component(
    vendor, raw_model: str, kind: ComponentKind
) -> Component | None:
    """Existing *model* component this raw string refers to.

    Deliberately ignores families: this answers "which specific part is
    this", which benchmarks need per model. Families are resolved separately
    by ``family_for_model``, so a curated family can never prevent the
    model-level entry a leaderboard ranks from existing.
    """
    from lumina.hardware.models import ComponentRole

    normalizer = NORMALIZERS[kind]
    cleaned = _comparable(raw_model, kind)
    if not cleaned:
        return None
    without_brand = _comparable(
        strip_vendor_prefix(normalizer(raw_model), vendor.name), kind
    )
    candidates = list(
        Component.objects.filter(
            kind=kind.value, vendor=vendor, role=ComponentRole.MODEL
        )
    )
    # exact (normalized) name or alias
    for component in candidates:
        names = [component.name] + list(
            (component.attributes or {}).get("aliases", [])
        )
        for name in names:
            comparable = _comparable(name, kind)
            if comparable and comparable in (cleaned, without_brand):
                return component
    return None


def catalog_name(vendor, raw_model: str, kind: ComponentKind) -> str:
    """The name a new catalog entry would get for this reported string.

    The translation, reported without applying it. lspci names the die and brackets the
    product, so "CometLake-S GT2 [UHD Graphics 630]" becomes "UHD Graphics 630" - and until
    this existed, a submitter looking at the preview was told a component would be created
    and never told under what name. The raw string was on screen; the thing that would end
    up in the catalog was not.

    Extracted from ``find_or_create_component`` rather than reimplemented, so a preview
    cannot promise one name and approval produce another.
    """
    if not (raw_model or "").strip():
        return ""
    normalized = NORMALIZERS[kind](raw_model)
    return strip_vendor_prefix(normalized, vendor.name if vendor is not None else "")


def find_or_create_component(
    vendor,
    raw_model: str,
    kind: ComponentKind,
    *,
    created_by=None,
    extra_attributes: dict | None = None,
) -> tuple[Component | None, bool]:
    """Return (component, created) for a raw model string under ``vendor``."""
    if not (raw_model or "").strip():
        return None, False
    existing = match_component(vendor, raw_model, kind)
    if existing is not None:
        return existing, False
    name = catalog_name(vendor, raw_model, kind)
    attributes = {"aliases": [raw_model]}
    attributes.update(extra_attributes or {})
    from lumina.hardware.models import ComponentRole

    component = Component.objects.create(
        vendor=vendor,
        name=name,
        kind=kind.value,
        role=ComponentRole.MODEL,   # auto-created entries are always models
        attributes=attributes,
        created_by=created_by,
    )
    return component, True


def find_or_create_cpu_component(vendor, raw_model, *, created_by=None):
    return find_or_create_component(
        vendor, raw_model, ComponentKind.cpu, created_by=created_by
    )
