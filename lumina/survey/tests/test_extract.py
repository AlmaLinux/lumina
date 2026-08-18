"""Extraction pulls facets and raw identity, and reads virtual/physical correctly."""
from __future__ import annotations

from lumina.survey import extract


def _inventory():
    return {"summary": {
        "system": {"vendor": "Dell Inc.", "product": "PowerEdge R760",
                   "uuid": "4c4c4544-0042-3510-8043-c2c04f313233", "serial": "ABC1234"},
        "baseboard": {"vendor": "Dell Inc.", "product": "0M83RH", "serial": "CN123456"},
        "cpus": [{"model": "Intel(R) Xeon(R) Gold 6430", "vendor": "GenuineIntel",
                  "sockets": 2, "cores": 64, "threads": 128, "flags": ["fpu", "vme"]}],
        "memory": {"total_bytes": 137438953472, "dimms": [{"type": "DDR5", "speed_mts": 4800}]},
        "gpus": [],
    }}


def test_extract_pulls_facets_and_raw_identity():
    environment = {"os": {"id": "almalinux", "version_id": "10.2", "arch": "x86_64",
                          "x86_64_level": "v3", "kernel": "6.12.0"}}
    cols = extract.survey_extract(_inventory(), environment)

    assert cols["cpu_model"] == "Intel Xeon Gold 6430"
    assert cols["cpu_sockets"] == 2
    assert cols["memory_bytes"] == 137438953472
    assert cols["memory_type"] == "DDR5"
    assert cols["board_model"] == "0M83RH"
    assert cols["arch"] == "x86_64"
    assert cols["x86_64_level"] == "v3"
    assert (cols["os_major"], cols["os_minor"]) == (10, 2)
    # Raw identity is kept - it is the survey's, dropped by the cert extractor.
    assert cols["system_uuid"].startswith("4c4c4544")
    assert cols["system_serial"] == "ABC1234"
    assert cols["board_serial"] == "CN123456"
    assert cols["virtual"] is False


def test_detects_virtual_from_hypervisor_flag():
    inventory = {"summary": {
        "cpus": [{"flags": ["fpu", "hypervisor"]}],
        "system": {"vendor": "QEMU", "product": "Standard PC (Q35 + ICH9, 2009)"},
    }}
    virtual, kind = extract.detect_virtualization(inventory)
    assert virtual is True
    assert kind == "qemu"


def test_physical_machine_is_not_virtual():
    inventory = {"summary": {
        "cpus": [{"flags": ["fpu", "vme"]}],
        "system": {"vendor": "Dell Inc.", "product": "PowerEdge R760"},
    }}
    assert extract.detect_virtualization(inventory) == (False, "")


def test_an_arm_vendor_publishes_without_its_trademark_mark():
    # The collector reads the vendor out of the BIOS fields on Arm, because lscpu's
    # "Vendor ID" there names the architecture licensor (ARM) and not who built the
    # chip. What the firmware hands over carries a mark - "Ampere(R)" - and the census
    # groups machines by this string, so it is normalized before it counts.
    inventory = _inventory()
    inventory["summary"]["cpus"] = [
        {"model": "Ampere(R) Altra(R) Processor", "vendor": "Ampere(R)",
         "sockets": 2, "cores": 160, "threads": 160, "flags": ["fp", "asimd"]},
    ]

    cols = extract.survey_extract(
        inventory, {"os": {"id": "almalinux", "version_id": "9.7", "arch": "aarch64"}}
    )

    assert cols["cpu_vendor"] == "Ampere"
    assert cols["cpu_model"] == "Ampere Altra"


def test_an_x86_vendor_key_is_left_alone():
    cols = extract.survey_extract(
        _inventory(), {"os": {"id": "almalinux", "version_id": "10.2", "arch": "x86_64"}}
    )

    assert cols["cpu_vendor"] == "GenuineIntel"
