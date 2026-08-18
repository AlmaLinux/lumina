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
    """``{"nics": [...], "gpus": [...]}`` for a run, categorized from its raw PCI enumeration.

    Falls back to the collector's already-categorized ``nics``/``gpus`` for a bundle that predates
    ``pci_devices``. Each returned device carries the raw ``pci_ids``/``class``/``driver`` plus any
    runtime enrichment joined by slot, so ``nic_identity``/``gpu_identity`` name it and the
    driver-bound check reads the real kernel driver.
    """
    summary = run.inventory.get("summary") or {}
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
