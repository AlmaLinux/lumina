"""Categorize a run's PCI devices server-side, from the raw enumeration the collector reports.

What counts as a NIC or a GPU is a decision, and decisions belong here, not in the collector - so a
card the collector's runtime view missed (a NIC the kernel bound a driver to but exposed under no
netdev the collector enumerated, say) is still categorized, and the rule can be corrected for
bundles already submitted rather than only for future runs. The suite now passes
``summary.pci_devices``: every ``lspci`` device with its class. NICs are class ``02``, GPUs class
``03``.

The collector's ``nics``/``gpus`` lists are kept as runtime *enrichment* - a NIC's live MAC/speed,
a GPU's ``nvidia-smi`` name - merged onto the categorized device by PCI slot. They are also the
fallback for a bundle written before ``pci_devices`` existed, so old runs read exactly as before.
"""

from __future__ import annotations

_NIC_CLASS_PREFIX = "02"
_GPU_CLASS_PREFIX = "03"

# Runtime fields worth lifting from the collector's view onto the categorized device. Identity
# (``pci_ids``), class, slot, and the bound driver come from the raw enumeration and are not
# overlaid by the (possibly emptier) view.
_NIC_ENRICHMENT = ("name", "mac", "operstate", "speed_mbps", "link", "driver_version", "firmware")
_GPU_ENRICHMENT = ("smi_name", "runtime", "vbios", "driver_version")


def _norm_slot(slot: str | None) -> str:
    """A PCI slot without its domain, so lspci's ``01:00.0`` and a netdev's ``0000:01:00.0`` join."""
    if not slot:
        return ""
    parts = str(slot).split(":")
    return ":".join(parts[-2:]) if len(parts) > 2 else str(slot)


def _merge(dev: dict, enrich: dict, extras: tuple) -> dict:
    merged = dict(dev)
    # The bound driver: lspci is authoritative, but fall back to the view's if lspci recorded none.
    merged["driver"] = dev.get("driver") or enrich.get("driver")
    for key in extras:
        value = enrich.get(key)
        if value not in (None, "", {}, []):
            merged[key] = value
    return merged


def categorized_devices(run) -> dict:
    """``{"nics": [...], "gpus": [...]}`` for anything carrying an ``inventory``."""
    return categorize_inventory(getattr(run, "inventory", None) or {})


def categorize_inventory(inventory: dict) -> dict:
    """``{"nics": [...], "gpus": [...]}``, categorized from a raw PCI enumeration.

    **The one place a reported device becomes "a GPU" or "a NIC".** Every consumer reads
    it from here: the device tables on a run page and a survey submission, the
    denormalized facets on a TestRun, and the survey rollup. That matters because the two
    sources disagree more often than it looks. ``pci_devices`` is the full enumeration and
    is authoritative; ``gpus``/``nics`` are the collector's own older categorization, and
    the two collectors fail independently - a machine whose GPU collector errored (a
    proprietary driver, a hung nvidia-smi) still enumerates the card under
    ``pci_devices`` while ``gpus`` comes back empty.

    A reader that consulted only ``summary["gpus"]`` therefore saw no GPU on exactly the
    machines that have an interesting one, which is the bug that produced a card visible
    on a submission's review page and absent from the published statistics. Prefer the
    enumeration, merge the collector's runtime enrichment in by slot, and fall back to the
    older lists only for a bundle written before ``pci_devices`` existed.

    Takes a plain inventory dict so a caller with no model instance can use it too;
    ``categorized_devices`` is the object-shaped wrapper.
    """
    summary = inventory.get("summary") or {}
    pci_devices = summary.get("pci_devices")
    if not pci_devices:
        return {
            "nics": list(summary.get("nics") or []),
            "gpus": list(summary.get("gpus") or []),
        }
    nic_view = {_norm_slot(n.get("pci")): n for n in (summary.get("nics") or [])}
    gpu_view = {_norm_slot(g.get("pci")): g for g in (summary.get("gpus") or [])}
    nics, gpus = [], []
    for dev in pci_devices:
        class_id = str(dev.get("class_id") or "")
        slot = _norm_slot(dev.get("pci"))
        if class_id.startswith(_NIC_CLASS_PREFIX):
            nics.append(_merge(dev, nic_view.get(slot, {}), _NIC_ENRICHMENT))
        elif class_id.startswith(_GPU_CLASS_PREFIX):
            gpus.append(_merge(dev, gpu_view.get(slot, {}), _GPU_ENRICHMENT))
    return {"nics": nics, "gpus": gpus}
