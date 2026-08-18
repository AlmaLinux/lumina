"""Which reported PCI devices are excluded from auto-attaching as components.

Two sources: a code rule (a display adapter from a non-accelerator vendor is a BMC/management
console) and admin-curated ComponentExclusionRule rows for specific parts. Both only decide the
default tick state; nothing here drops a device, so every case is overridable downstream.
"""
from __future__ import annotations

import pytest

from lumina.hardware.models import ComponentExclusionRule, ComponentKind
from lumina.results import exclusions

pytestmark = pytest.mark.django_db


def _gpu(vendor_id: str, device_id: str) -> dict:
    return {"pci_ids": {"vendor": f"Vendor [{vendor_id}]", "device": f"Chip [{device_id}]"}}


# --- categorical: BMC / management display adapters ------------------------------


def test_a_non_accelerator_display_vendor_is_excluded_as_management():
    # ASPEED (1a03) and Matrox (102b) make BMC console chips, not accelerators.
    for vendor in ("1a03", "102b"):
        reason = exclusions.exclusion_reason(_gpu(vendor, "2000"), ComponentKind.gpu, rules=[])
        assert reason == exclusions.MANAGEMENT_DISPLAY_REASON, vendor


def test_a_real_accelerator_vendor_is_not_categorically_excluded():
    for vendor in ("10de", "1002", "8086"):  # NVIDIA, AMD, Intel
        assert exclusions.exclusion_reason(_gpu(vendor, "2684"), ComponentKind.gpu, rules=[]) is None


def test_an_unidentifiable_display_device_is_left_for_a_human():
    # No vendor id to judge by: keep it rather than silently drop it.
    assert exclusions.exclusion_reason({"pci_ids": {}}, ComponentKind.gpu, rules=[]) is None


def test_the_categorical_rule_is_display_only_not_nics():
    # A non-accelerator vendor on a NIC is just a NIC vendor; the BMC rule is about display devices.
    assert exclusions.exclusion_reason(_gpu("1a03", "2000"), ComponentKind.nic, rules=[]) is None


# --- curated rows ----------------------------------------------------------------


def test_a_vendor_device_rule_matches_exactly_that_part():
    rule = ComponentExclusionRule.objects.create(
        vendor_id="1002", device_id="164E", kind="", reason="onboard iGPU",
    )
    # Stored lowercase, so it matches lspci's lowercase hex.
    assert rule.device_id == "164e"
    assert exclusions.exclusion_reason(_gpu("1002", "164e"), ComponentKind.gpu) == "onboard iGPU"
    # A different AMD GPU is untouched - this is not a blanket AMD exclusion.
    assert exclusions.exclusion_reason(_gpu("1002", "744c"), ComponentKind.gpu) is None


def _nic(vendor_id: str, device_id: str) -> dict:
    # NICs, so the categorical display rule never interferes with a curated-rule test.
    return {"pci_ids": {"vendor": f"Vendor [{vendor_id}]", "device": f"Chip [{device_id}]"}}


def test_a_vendor_only_rule_covers_the_whole_vendor():
    ComponentExclusionRule.objects.create(vendor_id="14e4", reason="every Broadcom NIC")
    assert exclusions.exclusion_reason(_nic("14e4", "1001"), ComponentKind.nic) == "every Broadcom NIC"
    assert exclusions.exclusion_reason(_nic("14e4", "9999"), ComponentKind.nic) == "every Broadcom NIC"


def test_a_rule_can_be_scoped_to_a_kind():
    # An accelerator vendor, so the GPU side is not swept up by the categorical rule; the point here
    # is that a gpu-scoped rule does not touch a NIC of the same vendor.
    ComponentExclusionRule.objects.create(
        vendor_id="10de", kind=ComponentKind.gpu.value, reason="some GPU",
    )
    assert exclusions.exclusion_reason(_gpu("10de", "2684"), ComponentKind.gpu) == "some GPU"
    assert exclusions.exclusion_reason(_nic("10de", "2684"), ComponentKind.nic) is None


def test_a_disabled_rule_is_ignored():
    ComponentExclusionRule.objects.create(vendor_id="14e4", reason="off", enabled=False)
    assert exclusions.exclusion_reason(_nic("14e4", "1001"), ComponentKind.nic) is None


def test_a_criteria_less_rule_never_matches_everything():
    # A footgun row with no vendor/device/kind must not blank every device.
    ComponentExclusionRule.objects.create(vendor_id="", device_id="", kind="", reason="oops")
    assert exclusions.exclusion_reason(_gpu("10de", "2684"), ComponentKind.gpu) is None
