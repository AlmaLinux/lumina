"""Extraction pulls facets and raw identity, and reads virtual/physical correctly."""
from __future__ import annotations

import pytest

from lumina.survey import extract

# ``survey_extract`` consults the catalog's vendor alias table and the census
# exclusion rules, so that the column it writes is the value the page shows.
pytestmark = pytest.mark.django_db


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


def test_the_vendor_column_holds_the_brand_the_page_shows():
    """It held "GenuineIntel", the CPUID key, while the statistics page showed "Intel" - the
    rollup applied the catalog's alias table and the column did not. A segment filtering on the
    vendor a reader can see therefore matched nothing. The column is an index over what is
    displayed, so it holds what is displayed."""
    cols = extract.survey_extract(
        _inventory(), {"os": {"id": "almalinux", "version_id": "10.2", "arch": "x86_64"}}
    )

    assert cols["cpu_vendor"] == "Intel"


# --- one namer, not two ----------------------------------------------------------
#
# Reported from a ThinkPad P14s Gen 5 AMD: the run page called the card "Radeon 780M" and the
# hardware statistics counted it as "HawkPoint1", the die codename, because the two paths read the
# same fields with different rules.

APU_INVENTORY = {
    "summary": {
        "cpus": [{"model": "AMD Ryzen 7 PRO 8840HS w/ Radeon 780M Graphics",
                  "vendor": "AuthenticAMD", "sockets": 1, "cores": 8, "threads": 16}],
        "gpus": [{
            "pci": "64:00.0", "driver": "amdgpu",
            "pci_ids": {"vendor": "Advanced Micro Devices, Inc. [AMD/ATI] [1002]",
                        "device": "HawkPoint1 [150e]"},
        }],
    },
}


def test_an_amd_apu_is_counted_under_its_marketing_name():
    assert extract.survey_extract(APU_INVENTORY, {})["gpu_model"] == "Radeon 780M"


def test_the_statistics_and_the_catalog_agree_on_the_same_machine():
    """The actual complaint: two readings of one machine. Asserted against the catalog's own
    namer rather than against a literal, so the two cannot drift apart again."""
    from lumina.results.component_match import gpu_display_name
    gpus = APU_INVENTORY["summary"]["gpus"]
    catalog = gpu_display_name(gpus[0], APU_INVENTORY["summary"]["cpus"][0]["model"], gpus)

    assert extract.survey_extract(APU_INVENTORY, {})["gpu_model"] == catalog


def test_a_discrete_card_keeps_the_name_pci_ids_gave_it():
    """The APU rule must not rename a card that pci.ids already brackets."""
    inventory = {"summary": {
        "cpus": APU_INVENTORY["summary"]["cpus"],
        "gpus": [{"pci": "03:00.0", "driver": "amdgpu",
                  "pci_ids": {"vendor": "Advanced Micro Devices, Inc. [AMD/ATI] [1002]",
                              "device": "Navi 33 [Radeon RX 7600]"}}],
    }}

    assert extract.survey_extract(inventory, {})["gpu_model"] == "Radeon RX 7600"


def test_two_unnamed_amd_cards_are_left_alone():
    """The CPU brand string names one iGPU, and with two candidates nothing says which."""
    unnamed = {"pci": "64:00.0", "driver": "amdgpu",
               "pci_ids": {"vendor": "Advanced Micro Devices, Inc. [AMD/ATI] [1002]",
                           "device": "HawkPoint1 [150e]"}}
    inventory = {"summary": {
        "cpus": APU_INVENTORY["summary"]["cpus"],
        "gpus": [unnamed, {**unnamed, "pci": "03:00.0",
                           "pci_ids": {**unnamed["pci_ids"], "device": "Phoenix1 [15bf]"}}],
    }}

    assert extract.survey_extract(inventory, {})["gpu_model"] == "HawkPoint1"


def test_an_nvidia_card_is_untouched_by_the_amd_rule():
    inventory = {"summary": {
        "cpus": APU_INVENTORY["summary"]["cpus"],
        "gpus": [{"pci": "01:00.0", "driver": "nvidia", "smi_name": "NVIDIA GeForce RTX 4090",
                  "pci_ids": {"vendor": "NVIDIA Corporation [10de]",
                              "device": "AD102 [GeForce RTX 4090]"}}],
    }}

    assert extract.survey_extract(inventory, {})["gpu_model"] == "NVIDIA GeForce RTX 4090"


# --- one machine, one name, wherever it is read ----------------------------------
#
# The defect this guards: a ThinkPad P14s was "Radeon 780M" on its run page and "HawkPoint1" in the
# statistics, because the catalog, the ingest column, and the rollup each named GPUs their own way.
# Asserting the three against each other rather than against a literal, so they cannot drift apart
# again without this failing.


def _submission(inventory):
    from lumina.survey.models import SurveySubmission

    return SurveySubmission(
        origin=SurveySubmission.ORIGIN_SURVEY,
        trust_tier=SurveySubmission.TIER_VERIFIED,
        inventory=inventory,
        **extract.survey_extract(inventory, {}),
    )


def test_the_catalog_the_column_and_the_rollup_name_the_same_card():
    from lumina.results.component_match import gpu_display_name
    from lumina.survey import facets

    gpus = APU_INVENTORY["summary"]["gpus"]
    catalog = gpu_display_name(gpus[0], APU_INVENTORY["summary"]["cpus"][0]["model"], gpus)
    sub = _submission(APU_INVENTORY)

    assert catalog == "Radeon 780M"
    assert sub.gpu_model == catalog, "the stored column disagrees with the catalog"
    assert facets.display_facets(sub)["gpu_model"] == [catalog], "the rollup disagrees"


def test_the_rollup_derives_the_cpu_model_rather_than_reading_the_column():
    """It read ``sub.cpu_model`` off the column, so a corrected CPU rule would have left every
    historical machine counted under the old parse for ever. The GPU bug was the same shape."""
    from lumina.survey import facets

    sub = _submission(APU_INVENTORY)
    sub.cpu_model = "whatever the column happens to say"

    assert facets.display_facets(sub)["cpu_model"] == ["AMD Ryzen 7 PRO 8840HS w/ Radeon 780M Graphics"]


def test_a_payload_with_no_devices_still_counts_under_its_columns():
    """A pruned or older payload is not a machine without parts. Dropping it out of the census
    would be a worse answer than counting what it did report."""
    from lumina.survey import facets

    sub = _submission(APU_INVENTORY)
    sub.inventory = {}

    counted = facets.display_facets(sub)

    assert counted["cpu_model"] == ["AMD Ryzen 7 PRO 8840HS w/ Radeon 780M Graphics"]
    assert counted["gpu_model"] == ["Radeon 780M"]


def test_a_rebuild_brings_a_stale_column_back_into_step():
    from lumina.survey import facets
    from lumina.survey.models import SurveySubmission

    sub = _submission(APU_INVENTORY)
    sub.save()
    SurveySubmission.objects.filter(pk=sub.pk).update(gpu_model="HawkPoint1")

    assert facets.rebuild() == 1
    sub.refresh_from_db()
    assert sub.gpu_model == "Radeon 780M"


def test_a_rebuild_leaves_the_payload_alone():
    """The line it must not cross."""
    from lumina.survey import facets
    from lumina.survey.models import SurveySubmission

    sub = _submission(APU_INVENTORY)
    sub.save()
    SurveySubmission.objects.filter(pk=sub.pk).update(gpu_model="HawkPoint1")

    facets.rebuild()
    sub.refresh_from_db()

    assert sub.inventory == APU_INVENTORY


def test_a_rebuild_with_nothing_to_do_changes_nothing():
    from lumina.survey import facets

    _submission(APU_INVENTORY).save()

    assert facets.rebuild() == 0


# --- a name that identifies nothing gets the CPU that does ------------------------
#
# "Intel Graphics" is what pci.ids calls every recent Intel integrated GPU, so one census bucket
# covered parts years apart and a reader could not tell what was in it. An integrated GPU has no
# identity apart from the chip it is on, so it borrows one. Only there: "Radeon 780M" and "UHD
# Graphics 630" are real parts that appear across several CPUs, and qualifying those would lose
# the grouping that makes them worth counting.


def _machine(cpu: str, device: str, vendor: str) -> dict:
    return {"summary": {
        "cpus": [{"model": cpu, "vendor": "GenuineIntel"}],
        "gpus": [{"pci": "00:02.0", "driver": "i915",
                  "pci_ids": {"vendor": vendor, "device": device}}],
    }}


INTEL = "Intel Corporation [8086]"
AMD = "Advanced Micro Devices, Inc. [AMD/ATI] [1002]"


def test_a_generic_intel_igpu_is_named_by_its_cpu():
    inventory = _machine("Intel(R) Core(TM) Ultra 9 275HX", "Arrow Lake-S [Intel Graphics]", INTEL)

    assert extract.survey_extract(inventory, {})["gpu_model"] == (
        "Intel Core Ultra 9 275HX (Intel Graphics)")


def test_the_borrowed_name_reads_like_the_cpu_facet_beside_it():
    """Normalized before it is borrowed: the marks, the clock speed, and the word CPU all go, so
    the label matches the ``cpu_model`` bucket rather than being a second spelling of it."""
    inventory = _machine(
        "Intel(R) Core(TM) i5-8250U CPU @ 1.60GHz", "Kaby Lake-R [Intel Graphics]", INTEL)
    columns = extract.survey_extract(inventory, {})

    assert columns["gpu_model"] == "Intel Core i5-8250U (Intel Graphics)"
    assert columns["gpu_model"].startswith(columns["cpu_model"])


def test_a_generic_amd_igpu_is_named_by_its_cpu_without_saying_graphics_twice():
    """The AMD brand string already carries the GPU, so the CPU half is trimmed at that boundary:
    "Ryzen 5 7520U with Radeon Graphics" plus "Radeon Graphics" would say it twice."""
    inventory = _machine("AMD Ryzen 5 7520U with Radeon Graphics", "Barcelo [Radeon Graphics]", AMD)

    assert extract.survey_extract(inventory, {})["gpu_model"] == (
        "AMD Ryzen 5 7520U (Radeon Graphics)")


def test_a_named_intel_igpu_keeps_its_own_name():
    """"UHD Graphics 630" is a part, and it appears across several CPUs. Qualifying it would split
    one real bucket into one per CPU and make the GPU dimension a copy of the CPU one."""
    inventory = _machine(
        "Intel(R) Core(TM) i7-8700 CPU @ 3.20GHz", "CoffeeLake-S GT2 [UHD Graphics 630]", INTEL)

    assert extract.survey_extract(inventory, {})["gpu_model"] == "UHD Graphics 630"


def test_a_named_amd_igpu_keeps_the_name_the_cpu_advertises():
    inventory = _machine(
        "AMD Ryzen 7 PRO 8840HS w/ Radeon 780M Graphics", "HawkPoint1 [150e]", AMD)

    assert extract.survey_extract(inventory, {})["gpu_model"] == "Radeon 780M"


def test_a_discrete_card_is_never_qualified():
    inventory = _machine(
        "Intel(R) Xeon(R) Gold 6430", "AD102 [GeForce RTX 4090]", "NVIDIA Corporation [10de]")

    assert extract.survey_extract(inventory, {})["gpu_model"] == "GeForce RTX 4090"


def test_a_generic_name_with_no_cpu_to_borrow_from_is_left_as_it_is():
    """Better a bucket that says little than one that says something invented."""
    inventory = _machine("", "Arrow Lake-S [Intel Graphics]", INTEL)

    assert extract.survey_extract(inventory, {})["gpu_model"] == "Intel Graphics"
