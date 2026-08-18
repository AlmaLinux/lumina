"""A survey submission's hardware, as devices rather than raw JSON.

The submission keeps its payload verbatim, which is the right thing to store and
the wrong thing to read: a reviewer deciding whether a report is plausible wants
a device list, not a blob. This assembles one.

Deliberately built on the pieces that already exist rather than a second set of
rules: ``results.device_inventory.categorized_devices`` decides what is a NIC or a
GPU (from ``summary.pci_devices``, keyed on PCI class, with the collector's runtime
view merged in by slot), and ``results.pci_names`` names every device. So a card
reads here exactly as it does on a run page and in the catalog, and a naming fix
lands in all of them at once.

``categorized_devices`` only needs ``.inventory``, so this works for any object
carrying one - a SurveySubmission today, a TestRun if it is ever wanted there.
"""
from __future__ import annotations

from lumina.results.device_inventory import categorize_inventory, categorized_devices
from lumina.results.exclusions import gpu_exclusion_reason
from lumina.results.pci_names import gpu_identity, nic_identity, pci_name
from lumina.vendors.pci_aliases import PCI_VENDOR_ALIASES

# Network (02) and display (03) devices get their own tables; everything else PCI
# enumerated lands in "other", which is the point of this view - the parts nothing
# else in the survey reports on.
_CATEGORIZED_CLASSES = ("02", "03")


def _summary(obj) -> dict:
    return (getattr(obj, "inventory", None) or {}).get("summary") or {}


def _nics(obj) -> list[dict]:
    out = []
    for dev in categorized_devices(obj).get("nics") or []:
        vendor, model = nic_identity(dev)
        out.append({
            "vendor": vendor,
            "model": model,
            "pci": dev.get("pci") or "",
            "driver": dev.get("driver") or "",
            "interface": dev.get("name") or "",
            "speed_mbps": dev.get("speed_mbps"),
            "link": dev.get("link"),
        })
    return out


def _gpus(obj) -> list[dict]:
    out = []
    # Fetched once for the whole submission rather than per device.
    from lumina.results.exclusions import active_rules

    rules = active_rules()
    for dev in categorized_devices(obj).get("gpus") or []:
        vendor, model = gpu_identity(dev)
        # Why a device is not in the statistics, shown beside it. Without this the page
        # listed every card a machine reported and the statistics quietly counted a
        # subset, and the only way to find out which was to read the exclusion rules by
        # hand. A reviewer looking at a card that is missing from the census should be
        # told here, on the card.
        excluded = census_exclusion_reason(dev, rules)
        out.append({
            "vendor": vendor,
            "model": model,
            "counted": excluded is None,
            "not_counted_because": excluded or "",
            "pci": dev.get("pci") or "",
            "driver": dev.get("driver") or "",
            "vbios": dev.get("vbios") or "",
            "smi_name": dev.get("smi_name") or "",
        })
    return out


def _other_pci(summary: dict) -> list[dict]:
    out = []
    for dev in summary.get("pci_devices") or []:
        if str(dev.get("class_id") or "").startswith(_CATEGORIZED_CLASSES):
            continue
        ids = dev.get("pci_ids") or {}
        out.append({
            "vendor": pci_name(ids.get("vendor")),
            "model": pci_name(ids.get("device")),
            "klass": dev.get("class") or "",
            "pci": dev.get("pci") or "",
            "driver": dev.get("driver") or "",
        })
    return out


def device_view(obj) -> dict:
    """Every device the submission reported, grouped for reading."""
    summary = _summary(obj)
    memory = summary.get("memory") or {}
    return {
        "nics": _nics(obj),
        "gpus": _gpus(obj),
        "other_pci": _other_pci(summary),
        "disks": list(summary.get("disks") or []),
        "dimms": list(memory.get("dimms") or []),
        "cpus": list(summary.get("cpus") or []),
        "chassis": summary.get("chassis") or {},
        "bmc": summary.get("bmc") or {},
        # Whether the bundle carried a full PCI enumeration at all: an old bundle
        # falls back to the collector's nic/gpu lists, and "no other devices" then
        # means "not reported", not "none present".
        "has_pci_enumeration": bool(summary.get("pci_devices")),
    }


def census_exclusion_reason(device: dict, rules: list | None = None) -> str | None:
    """Why the census should not count this device, or None.

    The catalog's rules, minus one class of them. A curated row is meant to name a part -
    "this onboard iGPU is not the accelerator under test" - and the census honours those,
    because a device not worth cataloguing is not worth counting either. A row with a
    ``kind`` and no vendor or device is a different animal: it says "never auto-attach any
    GPU", which is a catalog-wide switch about component creation, and reading it as "count
    no GPUs" would empty the whole dimension from one admin row. The categorical BMC rule
    still applies, since that one is about the part.
    """
    from lumina.results.exclusions import active_rules

    named = [
        rule for rule in (active_rules() if rules is None else rules)
        if rule.vendor_id or rule.device_id
    ]
    return gpu_exclusion_reason(device, named)


def countable_gpus(inventory, rules: list | None = None) -> list[tuple[str, str]] | None:
    """Every GPU this machine is counted under, as ``(vendor, model)`` pairs.

    **All of them, not the most interesting one.** A workstation with an integrated Intel
    display and a discrete NVIDIA card has both, and picking one meant picking wrong: the
    iGPU and the RTX card are both accelerator-vendor devices with a live driver, so the
    "prefer a discrete GPU" rule could not tell them apart and the machine was recorded as
    Intel while its RTX PRO 5000 appeared nowhere. Counting both removes the choice
    instead of trying to make it better, and answers the question a census is actually
    asked - how many AlmaLinux machines have an NVIDIA GPU - rather than how many have one
    as their *primary* display.

    Deduplicated, so four identical A100s in one box count that box once. Ordered as
    enumerated, so the reading is stable.

    Excluded devices are dropped: a BMC console by the categorical rule, and any part a
    reviewer has blacklisted. ``None`` means the payload carries no device enumeration at
    all, which is not the same as "no GPU" - there is nothing to apply a rule to, so the
    caller should keep what was extracted at ingest.
    """
    summary = ((inventory or {}).get("summary") or {})
    # The categorizer, so a card visible on the submission's review page is necessarily
    # the card counted here. Reading ``summary["gpus"]`` directly is what broke that: the
    # review page has always gone through ``categorized_devices``, which prefers the full
    # ``pci_devices`` enumeration, and the GPU collector fails independently of the PCI
    # one - so a machine with a working NVIDIA card showed it on the page and contributed
    # nothing to the GPU statistics.
    devices = [
        gpu for gpu in categorize_inventory(inventory or {})["gpus"]
        if isinstance(gpu, dict)
    ]
    if not devices:
        if "pci_devices" not in summary and "gpus" not in summary:
            return None
        return []

    out: list[tuple[str, str]] = []
    for gpu in devices:
        if census_exclusion_reason(gpu, rules) is not None:
            continue
        vendor, model = gpu_identity(gpu)
        pair = (_vendor_name(vendor), model)
        if pair not in out:
            out.append(pair)
    return out


def countable_gpu(inventory, rules: list | None = None) -> tuple[str, str] | None:
    """The first GPU this machine is counted under, or ``("", "")`` for none.

    Kept for the callers that want one answer to show a person. The statistics count
    every GPU: see ``countable_gpus``.
    """
    gpus = countable_gpus(inventory, rules)
    if gpus is None:
        return None
    return gpus[0] if gpus else ("", "")


# pci.ids spells the silicon vendors at length: "Advanced Micro Devices, Inc. [AMD/ATI]".
# The catalog already keeps the mapping to what it calls them, so the census reads the same
# table rather than a second one that could disagree about who a company is. Unmapped
# spellings pass through, which is the table's own rule: a guessed mapping that is wrong
# merges two companies, and that is worse than one long label.
_VENDOR_NAMES = {spelling.casefold(): name for spelling, name in PCI_VENDOR_ALIASES}


def _vendor_name(vendor: str) -> str:
    return _VENDOR_NAMES.get((vendor or "").strip().casefold(), vendor)
