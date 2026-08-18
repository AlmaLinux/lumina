"""What to call a part, from the names lspci and its friends reported.

The collectors report every name they were given, verbatim, and choose between them nowhere. Every
choice lives here instead:

    "the collector shouldn't really make any decisions. It's just collecting and reporting.
     Keeping the raw data and decisions server-side in lumina helps us in case of issues where we
     need to reprocess data in some way."

The reason, concretely. A bundle is written once and read for years. A rule that lives in the
reader can be corrected for every bundle ever submitted; one applied at collection time is frozen
into each of them. The NIC collector chose a name and gave two real NICs the model "Device", and no
server-side fix could recover the right answer for the bundle already on disk - the string it
needed had been thrown away before the report was written.

A module of its own because two callers need it and neither can import the other:
``results.services`` builds the catalog ties, and ``results.inventory_extract`` fills the
denormalized ``gpu_model`` column that the run pages and leaderboards read.
"""
from __future__ import annotations

import re

# Trailing "[10ec]" / "[8168]" that lspci's -nn appends to every name. The bundle keeps them; a
# reader that distrusts a name can still look the number up.
_PCI_ID_SUFFIX_RE = re.compile(r"\s*\[[0-9a-f]{4}\]$")
# What lspci writes when pci.ids has no name for an id: the word "Device" (or "Unknown device")
# and the bare number. A placeholder, not a product, and extremely common for the *subsystem* of
# an onboard part - a Dell-integrated Realtek NIC reports "SDevice: Device [09a8]" because nobody
# has catalogued Dell's subsystem id for it.
#
# Reported from a real run: the Realtek NIC arrived as model "Device", and so did the Intel Wi-Fi
# beside it. The collector was choosing the subsystem name and had no reason to distrust it.
_PCI_PLACEHOLDER_RE = re.compile(r"^(?:unknown\s+)?device(?:\s+[0-9a-f]{4})?$", re.I)

# The tokens the GPU collector wrote before it started reporting lspci's strings verbatim. Kept
# for bundles submitted while it did.
GPU_VENDOR_NAMES = {
    "nvidia": "NVIDIA",
    "amd": "AMD",
    "intel": "Intel",
    "aspeed": "ASPEED",
    "matrox": "Matrox",
}


# PCI vendor ids for the three chip makers whose display-class devices are accelerators. Anything
# else on that class is a management adapter driving a VGA console nobody looks at.
#
# On the id rather than the name, because the names are whatever pci.ids says and that has already
# caught this project out once: AMD ships as "Advanced Micro Devices, Inc. [AMD/ATI]", which matches
# no token anybody would write. The id is four hex digits and does not get reworded.
ACCELERATOR_VENDOR_IDS = frozenset({"10de", "1002", "8086"})
_PCI_VENDOR_ID_RE = re.compile(r"\[([0-9a-f]{4})\]\s*$")


def pci_vendor_id(device: dict) -> str:
    """The PCI vendor id of a reported device, lowercase, or "".

    From ``pci_ids.vendor``, which the collector records exactly as lspci printed it:
    ``"NVIDIA Corporation [10de]"``. Falls back to the token the collector used to flatten, so a
    bundle submitted before that changed still resolves.
    """
    ids = device.get("pci_ids") or {}
    match = _PCI_VENDOR_ID_RE.search(ids.get("vendor") or "")
    if match:
        return match.group(1).lower()
    legacy = (device.get("vendor") or "").strip().lower()
    return {"nvidia": "10de", "amd": "1002", "intel": "8086",
            "aspeed": "1a03", "matrox": "102b"}.get(legacy, "")


def pci_device_id(device: dict) -> str:
    """The PCI device id of a reported device, lowercase, or "".

    lspci -nn prints it as the trailing ``[7d55]`` on the device string, after the die/product
    name, which may itself carry brackets (``"AD102GL [L40S] [26b9]"``). The last one is the id.
    No collector helper ever extracted this, so unlike the vendor there is no flattened fallback.
    """
    ids = device.get("pci_ids") or {}
    match = _PCI_VENDOR_ID_RE.search(ids.get("device") or "")
    return match.group(1).lower() if match else ""


# Which chip a clpeak device name belongs to, by the words the drivers put in it. Only used to pick
# among a system's GPUs when there is more than one, so a coarse vendor answer is enough. AMD's
# Vulkan name carries "RADV" and its ROCm name is a bare "gfx<n>" with no vendor word at all.
_CLPEAK_VENDOR_KEYWORDS = (
    ("10de", ("nvidia", "geforce", "quadro", "tesla")),
    ("1002", ("amd", "radeon", "radv", "instinct")),
    ("8086", ("intel",)),
)


def _clpeak_vendor_id(name: str) -> str:
    low = (name or "").lower()
    if low.startswith("gfx"):
        return "1002"
    for vendor_id, keywords in _CLPEAK_VENDOR_KEYWORDS:
        if any(word in low for word in keywords):
            return vendor_id
    return ""


def benchmark_gpu_pci_id(device_raw: str, gpus: list) -> str:
    """The ``"vendor:device"`` PCI key of the inventory GPU a clpeak benchmark device ran on, or "".

    clpeak names a device per backend and its result carries no PCI id of its own, so the same
    physical card is named differently by Vulkan and OpenCL (``"Intel Graphics (ARL)"`` against a
    bare ``"Intel Graphics"``) and splits into two entries in the catalog. The inventory GPUs, by
    contrast, carry the PCI ids. This ties a benchmark row back to the card it ran on so its
    backends group as one thing.

    Model-level (``vendor:device``): two physically identical cards share it and ``device_ordinal``
    tells their instances apart, which is the right granularity for a leaderboard that ranks a
    model across machines. Deliberately conservative: it returns "" - and the caller falls back to
    the raw ``device_model`` string, splitting exactly as before - whenever the card cannot be
    pinned down without guessing. That is when the run has more than one *different* GPU of the
    device's vendor, or names a vendor the inventory does not carry at all. A run with one GPU of
    that vendor (the common case, including this Intel iGPU) resolves cleanly whatever the backend
    called it.
    """
    accel = [g for g in (gpus or []) if pci_vendor_id(g) in ACCELERATOR_VENDOR_IDS]
    if not accel:
        return ""
    vendor_id = _clpeak_vendor_id(device_raw)
    if vendor_id:
        candidates = [g for g in accel if pci_vendor_id(g) == vendor_id]
        if not candidates:
            # The device names a vendor no inventory GPU has; tying it to a different vendor's card
            # would be worse than leaving it on its own device_model string.
            return ""
    else:
        candidates = accel
    # Every candidate must contribute the same key. The ``all(...)`` guard matters as much as the
    # set size: a same-vendor sibling with no device id in its pci_ids would otherwise be dropped
    # from the set, and a one-id/one-idless pair would collapse to a single key and mis-tie the row
    # to the identified card. An unidentifiable candidate means "cannot pin down" -> blank.
    if not all(pci_device_id(gpu) for gpu in candidates):
        return ""
    keys = {f"{pci_vendor_id(gpu)}:{pci_device_id(gpu)}" for gpu in candidates}
    return keys.pop() if len(keys) == 1 else ""


def pci_name(value) -> str:
    """An lspci name with its id suffix removed, or "" where lspci had no name to give."""
    cleaned = _PCI_ID_SUFFIX_RE.sub("", (value or "").strip()).strip()
    return "" if _PCI_PLACEHOLDER_RE.match(cleaned) else cleaned


def nic_identity(nic: dict) -> tuple[str, str]:
    """The ``(vendor, model)`` to catalog a reported NIC under.

    Every name lspci gave is in the bundle and the choosing happens here, so a rule that turns
    out wrong is corrected for every bundle ever submitted rather than only for runs made after
    the fix. That is not a hypothetical: the first version of this chose in the collector and gave
    two real NICs the model "Device".

    **Vendor: the chip's, not the card's.** The same Realtek silicon is soldered onto boards from
    a dozen brands, so a Dell-branded onboard Realtek is catalogued as the Realtek. "Dell" in
    ``subsystem_vendor`` is deliberately ignored even when it is a real name.

    **Model: the chip's ``device``, i.e. the controller.** This is the controller model - "Ethernet
    Connection X722 for 1GbE", "Ethernet Controller X710 for 10GbE SFP+" - the same string lshw
    reports as the port's ``product``, and the identity that actually decides driver support and
    certification: two differently branded cards on one controller are the same silicon to certify.
    ``subsystem_device`` is deliberately *not* preferred: for an onboard NIC it is the motherboard
    (a Supermicro NIC reported the board "X11DPi-N"), and neither a board-model nor a board-vendor
    match tells an onboard part from a card reliably - the subsystem can name a different board in
    the same family, and an Intel card in an Intel-made board shares the board's vendor. It is kept
    only as a fallback for the rare controller whose own ``device`` is a bare pci.ids placeholder.

    Bundles from before the collector reported ``pci_ids`` carry flattened ``vendor`` and ``model``
    keys instead; those are read as a last resort, still through ``pci_name`` so a stored
    "Device" is rejected rather than catalogued.
    """
    ids = nic.get("pci_ids") or {}
    vendor = pci_name(ids.get("vendor")) or pci_name(nic.get("vendor"))
    model = (
        pci_name(ids.get("device"))
        or pci_name(ids.get("subsystem_device"))
        or pci_name(nic.get("model"))
    )
    return vendor, model


def gpu_identity(gpu: dict) -> tuple[str, str]:
    """The ``(vendor, model)`` to catalog a reported GPU under.

    The GPU collector used to decide this: it mapped the PCI vendor id through a five-entry table
    to a token ("nvidia"), stripped the ids off the device name, and let nvidia-smi's marketing
    name overwrite lspci's die name. All three are judgements, and a bundle is written once and
    read for years - so they belong here, where a rule that turns out wrong can be corrected for
    every bundle ever submitted. The NIC collector made the same mistake first, and produced a
    catalog component called "Device".

    Model, in order: nvidia-smi's name where there is one, because it is the marketing name and
    the die name is not ("NVIDIA GeForce RTX 4090" against "AD102 [GeForce RTX 4090]"); then the
    subsystem name, which is the board a partner shipped; then lspci's device name, which
    ``normalize_gpu_model`` reads brackets out of.

    Vendor, unlike NICs: the *chip* vendor again, and here that is uncontroversial - an ASUS-built
    GeForce is an NVIDIA GPU, and certification applies to the family NVIDIA defines.

    Older bundles carry the flattened ``vendor``/``model`` pair instead. Read as a last resort,
    with ``GPU_VENDOR_NAMES`` translating the token they stored.
    """
    ids = gpu.get("pci_ids") or {}
    stored = (gpu.get("vendor") or "").strip()
    vendor = pci_name(ids.get("vendor")) or GPU_VENDOR_NAMES.get(stored.lower(), stored)
    model = (
        pci_name(gpu.get("smi_name"))
        or pci_name(ids.get("subsystem_device"))
        or pci_name(ids.get("device"))
        or pci_name(gpu.get("model"))
    )
    return vendor, model
