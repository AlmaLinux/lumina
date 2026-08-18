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

from lumina.results import inventory_extract as inv
from lumina.results.device_inventory import categorized_devices
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
    for dev in categorized_devices(obj).get("gpus") or []:
        vendor, model = gpu_identity(dev)
        out.append({
            "vendor": vendor,
            "model": model,
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


def countable_gpu(inventory, rules: list | None = None) -> tuple[str, str] | None:
    """The GPU this machine is counted under in the statistics.

    A server almost always reports a display adapter, because the BMC provides one, and
    a BMC console is not what anybody means by "which GPU does this machine have". The
    catalog already decides this for certification (``results.exclusions``: a display
    device from a vendor that makes no accelerators, plus admin-curated rows for the
    specific parts). The census asks the same question, so it takes the same answer: a
    device excluded from validation is not relevant to the statistics either.

    A machine whose only graphics is a management adapter counts under **no** GPU vendor
    rather than under the adapter's, which is why this returns a blank pair rather than
    falling back. That drops it from the GPU dimensions entirely, the way a machine that
    reported no x86-64 level is absent from that one, and leaves those percentages as
    shares of the machines that actually have a GPU.

    Three answers, not two. ``("NVIDIA Corporation", "...")`` is a countable GPU;
    ``("", "")`` means the machine listed graphics and none of it counts; ``None`` means
    the payload listed no GPUs *at all*, which is not the same claim - there is nothing
    to apply a rule to, so the caller should fall back to whatever was extracted at
    ingest rather than silently recording that the machine has no GPU.

    ``rules`` comes from ``exclusions.active_rules()``; pass it to avoid a query per
    machine when counting a whole period.
    """
    summary = ((inventory or {}).get("summary") or {})
    if "gpus" not in summary:
        return None
    gpus = [gpu for gpu in (summary.get("gpus") or []) if isinstance(gpu, dict)]
    if not gpus:
        return "", ""
    # The primary first: on a machine with both an accelerator and a BMC adapter that is
    # the accelerator, and asking about it first keeps the ordering rule in one place.
    primary = inv._primary_gpu(gpus)
    ordered = [primary, *(g for g in gpus if g is not primary)] if primary else gpus
    for gpu in ordered:
        if gpu_exclusion_reason(gpu, rules) is None:
            vendor, model = gpu_identity(gpu)
            return _vendor_name(vendor), model
    return "", ""


# pci.ids spells the silicon vendors at length: "Advanced Micro Devices, Inc. [AMD/ATI]".
# The catalog already keeps the mapping to what it calls them, so the census reads the same
# table rather than a second one that could disagree about who a company is. Unmapped
# spellings pass through, which is the table's own rule: a guessed mapping that is wrong
# merges two companies, and that is worse than one long label.
_VENDOR_NAMES = {spelling.casefold(): name for spelling, name in PCI_VENDOR_ALIASES}


def _vendor_name(vendor: str) -> str:
    return _VENDOR_NAMES.get((vendor or "").strip().casefold(), vendor)
