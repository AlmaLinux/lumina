"""Server-side PCI categorization: NIC = class 02, GPU = class 03, from the raw enumeration.

The point of the change is that the collector reports raw devices and lumina decides what they are,
so the tests centre on: a NIC the collector's netdev view would have missed is still categorized,
enrichment is merged by slot, and a bundle without ``pci_devices`` falls back to the old lists.
"""
from __future__ import annotations

from types import SimpleNamespace

from lumina.results.device_inventory import categorized_devices


def _run(summary: dict):
    return SimpleNamespace(inventory={"summary": summary})


# Real slices from a reported machine: a Mellanox NIC (driver bound), the ASPEED BMC display, and an
# AMD iGPU. class_id is what categorization keys on.
MLX = {
    "pci": "01:00.0", "class": "Ethernet controller [0200]", "class_id": "0200",
    "pci_ids": {"vendor": "Mellanox Technologies [15b3]",
                "device": "MT27710 Family [ConnectX-4 Lx] [1015]"},
    "driver": "mlx5_core",
}
ASPEED = {
    "pci": "0c:00.0", "class": "VGA compatible controller [0300]", "class_id": "0300",
    "pci_ids": {"vendor": "ASPEED Technology, Inc. [1a03]",
                "device": "ASPEED Graphics Family [2000]"},
    "driver": "ast",
}
IGPU = {
    "pci": "11:00.0", "class": "VGA compatible controller [0300]", "class_id": "0300",
    "pci_ids": {"vendor": "Advanced Micro Devices, Inc. [AMD/ATI] [1002]",
                "device": "Raphael [164e]"},
    "driver": "amdgpu",
}


def test_devices_are_categorized_by_pci_class():
    got = categorized_devices(_run({"pci_devices": [MLX, ASPEED, IGPU]}))
    assert [n["pci"] for n in got["nics"]] == ["01:00.0"]           # class 02
    assert sorted(g["pci"] for g in got["gpus"]) == ["0c:00.0", "11:00.0"]  # class 03


def test_a_nic_missing_from_the_netdev_view_is_still_categorized():
    """The reported bug: the Mellanox is in pci_devices with mlx5_core bound, so it is a NIC even
    with no matching entry in the collector's netdev ``nics`` view."""
    got = categorized_devices(_run({"pci_devices": [MLX], "nics": []}))
    assert len(got["nics"]) == 1
    nic = got["nics"][0]
    assert nic["pci_ids"]["vendor"] == "Mellanox Technologies [15b3]"
    assert nic["driver"] == "mlx5_core"


def test_runtime_data_is_merged_from_the_view_by_slot():
    """The netdev view supplies the live facts lspci lacks - MAC, speed, link - joined by slot even
    when the view's slot carries the PCI domain and the raw one does not."""
    view_nic = {"pci": "0000:01:00.0", "name": "enp1s0f0np0", "mac": "24:8a:07:ab:c0:88",
                "speed_mbps": 25000, "link": True, "firmware": "14.32.1010"}
    got = categorized_devices(_run({"pci_devices": [MLX], "nics": [view_nic]}))
    nic = got["nics"][0]
    assert nic["mac"] == "24:8a:07:ab:c0:88"
    assert nic["speed_mbps"] == 25000 and nic["link"] is True
    assert nic["firmware"] == "14.32.1010"
    # Identity is still the raw one, not clobbered by the view.
    assert nic["pci_ids"]["device"] == "MT27710 Family [ConnectX-4 Lx] [1015]"


def test_lspci_driver_wins_but_the_view_fills_a_gap():
    # lspci is authoritative for the bound driver; the view's is a fallback only.
    got = categorized_devices(_run({"pci_devices": [MLX], "nics": [{"pci": "01:00.0",
                                                                    "driver": "something-else"}]}))
    assert got["nics"][0]["driver"] == "mlx5_core"
    driverless = {**MLX, "driver": None}
    got = categorized_devices(_run({"pci_devices": [driverless],
                                    "nics": [{"pci": "01:00.0", "driver": "mlx5_core"}]}))
    assert got["nics"][0]["driver"] == "mlx5_core"


def test_a_bundle_without_pci_devices_falls_back_to_the_old_lists():
    legacy = {"nics": [{"pci": "09:00.0", "driver": "igb"}],
              "gpus": [{"pci": "0c:00.0", "driver": "ast"}]}
    got = categorized_devices(_run(legacy))
    assert got["nics"] == legacy["nics"]
    assert got["gpus"] == legacy["gpus"]
