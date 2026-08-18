"""The submission's hardware as devices: categorized from PCI, named by the shared rules."""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.urls import reverse

from lumina.survey.devices import device_view
from lumina.survey.models import SurveySubmission

pytestmark = pytest.mark.django_db
User = get_user_model()


def _inventory():
    return {"summary": {
        "pci_devices": [
            {"pci": "01:00.0", "class": "Ethernet controller", "class_id": "0200",
             "pci_ids": {"vendor": "Intel Corporation [8086]",
                         "device": "Ethernet Connection X722 for 1GbE [37d1]"},
             "driver": "i40e"},
            {"pci": "02:00.0", "class": "VGA compatible controller", "class_id": "0300",
             "pci_ids": {"vendor": "NVIDIA Corporation [10de]",
                         "device": "AD102GL [L40S] [26b9]"},
             "driver": "nvidia"},
            {"pci": "00:17.0", "class": "SATA controller", "class_id": "0106",
             "pci_ids": {"vendor": "Intel Corporation [8086]",
                         "device": "C620 Series Chipset SATA Controller [a182]"},
             "driver": "ahci"},
        ],
        # The runtime view, joined onto the enumeration by slot (note the domain prefix).
        "nics": [{"pci": "0000:01:00.0", "name": "eno1", "speed_mbps": 1000}],
        "gpus": [{"pci": "0000:02:00.0", "smi_name": "NVIDIA L40S", "vbios": "95.02.66"}],
        "disks": [{"name": "nvme0n1", "transport": "nvme", "bytes": 1000204886016}],
        "memory": {"dimms": [{"locator": "DIMM_A1", "size_bytes": 34359738368,
                              "type": "DDR5", "speed_mts": 4800,
                              "manufacturer": "Micron", "part_number": "MTC20F2085"}]},
        "cpus": [{"model": "AMD EPYC 9354", "vendor": "AuthenticAMD",
                  "sockets": 1, "cores": 32, "threads": 64}],
        "chassis": {"type": "Rack Mount Chassis"},
    }}


def _sub(**kw) -> SurveySubmission:
    return SurveySubmission.objects.create(
        origin=SurveySubmission.ORIGIN_SURVEY,
        trust_tier=SurveySubmission.TIER_VERIFIED,
        **kw,
    )


def _reviewer():
    rev = User.objects.create_user(username="rev", password="x")
    rev.groups.add(Group.objects.get_or_create(name="reviewer")[0])
    return rev


def test_nics_are_named_by_the_shared_rule_and_joined_to_the_runtime_view():
    view = device_view(_sub(inventory=_inventory()))

    nic = view["nics"][0]
    assert nic["vendor"] == "Intel Corporation"
    # Device-first naming: the controller, not the board's subsystem id.
    assert nic["model"] == "Ethernet Connection X722 for 1GbE"
    assert nic["driver"] == "i40e"
    assert nic["interface"] == "eno1"       # runtime enrichment joined across the domain prefix
    assert nic["speed_mbps"] == 1000


def test_gpus_are_categorized_and_named():
    view = device_view(_sub(inventory=_inventory()))
    gpu = view["gpus"][0]
    assert "L40S" in gpu["model"]
    assert gpu["driver"] == "nvidia"
    assert gpu["vbios"] == "95.02.66"


def test_other_pci_devices_are_the_ones_nothing_else_reports():
    view = device_view(_sub(inventory=_inventory()))

    assert len(view["other_pci"]) == 1      # the NIC and GPU have their own tables
    sata = view["other_pci"][0]
    assert sata["klass"] == "SATA controller"
    assert sata["model"] == "C620 Series Chipset SATA Controller"
    assert sata["driver"] == "ahci"
    assert view["has_pci_enumeration"] is True


def test_the_rest_of_the_hardware_is_grouped():
    view = device_view(_sub(inventory=_inventory()))
    assert view["disks"][0]["name"] == "nvme0n1"
    assert view["dimms"][0]["locator"] == "DIMM_A1"
    assert view["cpus"][0]["model"] == "AMD EPYC 9354"
    assert view["chassis"]["type"] == "Rack Mount Chassis"


def test_a_bundle_without_pci_enumeration_falls_back():
    inventory = {"summary": {"nics": [{"vendor": "Realtek Semiconductor Co., Ltd. [10ec]",
                                       "model": "RTL8111 [8168]", "name": "enp2s0"}]}}
    view = device_view(_sub(inventory=inventory))

    assert view["has_pci_enumeration"] is False   # so an empty "other" means not reported
    assert len(view["nics"]) == 1
    assert view["other_pci"] == []


def test_the_detail_page_renders_the_device_tables(client):
    sub = _sub(inventory=_inventory())
    client.force_login(_reviewer())

    body = client.get(
        reverse("review:survey_submission_detail", args=[sub.pk])
    ).content.decode()

    assert "Ethernet Connection X722 for 1GbE" in body
    assert "C620 Series Chipset SATA Controller" in body   # the rest of the hardware
    assert "SATA controller" in body
    assert "nvme0n1" in body
    assert "DIMM_A1" in body
