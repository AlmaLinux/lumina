"""Which reported PCI devices the catalog should not auto-attach as components.

Two sources, one answer. A blanket rule in code - a display adapter from a vendor that makes no
accelerators is a management/BMC console nobody certifies, never hardware under test - plus
admin-curated ``ComponentExclusionRule`` rows for the specific parts that *are* real accelerators or
NICs yet still not worth cataloguing (an onboard iGPU, say).

A match returns the reason to show the reviewer. The device is kept verbatim in the inventory; the
exclusion only unticks it by default on the review screen, and a reviewer can tick it to include it
anyway. So this decides a default, never a hard drop - which is why every rule is overridable and
why the collector still reports the device.
"""

from __future__ import annotations

from lumina.hardware.models import ComponentExclusionRule, ComponentKind
from lumina.results.pci_names import (
    ACCELERATOR_VENDOR_IDS,
    pci_device_id,
    pci_vendor_id,
)

# Shown for the categorical rule. The specific-part rows carry their own reason.
MANAGEMENT_DISPLAY_REASON = (
    "management display adapter (BMC console), not an accelerator under test"
)


def active_rules() -> list[ComponentExclusionRule]:
    """The enabled curated rules, fetched once so a run's devices share one query."""
    return list(ComponentExclusionRule.objects.filter(enabled=True))


def _kind_value(kind) -> str:
    return kind.value if isinstance(kind, ComponentKind) else str(kind)


def exclusion_reason(device: dict, kind, rules: list | None = None) -> str | None:
    """Why this device should be excluded by default, or None to keep it ticked.

    ``device`` is an inventory entry carrying ``pci_ids`` (a gpu or nic dict); ``kind`` is the
    ``ComponentKind`` it was catalogued under. Pass ``rules`` (from ``active_rules``) to avoid a
    query per device when checking a whole run.
    """
    vendor = pci_vendor_id(device)
    device_id = pci_device_id(device)
    kind_value = _kind_value(kind)

    # Categorical: a display device whose vendor makes no accelerators is a BMC/management adapter.
    # Uses the same non-accelerator knowledge pci_names already relies on, so ASPEED (1a03), Matrox
    # (102b), and the like are covered without a row each. Only when the vendor is known - an
    # unidentifiable display device is left for a human to judge rather than silently dropped.
    if kind_value == ComponentKind.gpu.value and vendor and vendor not in ACCELERATOR_VENDOR_IDS:
        return MANAGEMENT_DISPLAY_REASON

    for rule in (active_rules() if rules is None else rules):
        # A row with no vendor/device/kind would match every device; ignore it rather than blank
        # the whole run (the model rejects creating one, but an old row or a fixture could exist).
        if not (rule.vendor_id or rule.device_id or rule.kind):
            continue
        if rule.vendor_id and rule.vendor_id != vendor:
            continue
        if rule.device_id and rule.device_id != device_id:
            continue
        if rule.kind and rule.kind != kind_value:
            continue
        return rule.reason
    return None


def gpu_exclusion_reason(device: dict, rules: list | None = None) -> str | None:
    """``exclusion_reason`` for a GPU, without the caller naming a ``ComponentKind``.

    Exists for the survey, which counts machines by their GPU and must apply the same
    judgement the review screen does: a device nobody would catalogue is a device nobody
    wants in the statistics either. Spelling ``ComponentKind.gpu`` there would mean the
    census importing the catalog, and passing the bare string "gpu" would couple the two
    through a literal that no test would catch if it changed.
    """
    return exclusion_reason(device, ComponentKind.gpu, rules)
