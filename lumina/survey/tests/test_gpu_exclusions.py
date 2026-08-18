"""The statistics count the GPU a person would name, not the BMC's display adapter.

Almost every server reports a display adapter, because the baseboard management
controller provides one, and ASPEED and Matrox therefore top a naive count of GPU
vendors. Nobody buying or specifying a server means that when they ask what GPU it has.

The catalog already decides this for certification: ``results.exclusions`` holds a
categorical rule (a display device from a vendor that makes no accelerators is a
management console) plus admin-curated rows for specific parts. The census takes the same
answer rather than a second opinion, because a device excluded from validation is not
relevant to the statistics either, and two rule sets would drift.

Applied in the **rollup**, not at ingest, so it reaches submissions already stored: the
next rollup recounts everything ever submitted, and an exclusion row added tomorrow
corrects yesterday's numbers. That is what the survey's raw-in-aggregate-out shape was
for. The stored facet column keeps saying what the machine reported.
"""
from __future__ import annotations

import datetime as dt

import pytest

from lumina.hardware.models import ComponentExclusionRule
from lumina.survey import services, stats
from lumina.survey.devices import countable_gpu
from lumina.survey.models import SurveySubmission

pytestmark = pytest.mark.django_db

_ASPEED = {"pci_ids": {"vendor": "ASPEED Technology, Inc. [1a03]", "device": "ASPEED Graphics Family [2000]"},
           "name": "ASPEED Graphics Family"}
_MATROX = {"pci_ids": {"vendor": "Matrox Electronics Systems Ltd. [102b]", "device": "G200eR2 [0534]"},
           "name": "G200eR2"}
_NVIDIA = {"pci_ids": {"vendor": "NVIDIA Corporation [10de]", "device": "GA102GL [RTX A6000] [2230]"},
           "driver": "nvidia", "name": "GA102GL [RTX A6000]"}
_INTEL_IGPU = {"pci_ids": {"vendor": "Intel Corporation [8086]", "device": "AlderLake-S GT1 [4680]"},
               "driver": "i915", "name": "AlderLake-S GT1"}


def _inventory(*gpus):
    return {"summary": {"gpus": list(gpus)}}


def _sub(*, when=None, **kw):
    defaults = dict(
        origin=SurveySubmission.ORIGIN_SURVEY,
        trust_tier=SurveySubmission.TIER_VERIFIED,
    )
    defaults.update(kw)
    sub = SurveySubmission.objects.create(**defaults)
    if when:
        SurveySubmission.objects.filter(pk=sub.pk).update(received_at=when)
    return sub


def _at(year, month, day=15):
    return dt.datetime(year, month, day, 12, tzinfo=dt.UTC)


# --- picking the countable GPU ---------------------------------------------------

def test_a_management_adapter_alone_counts_as_no_gpu():
    # Not "counts as ASPEED". The machine has no GPU anybody means.
    assert countable_gpu(_inventory(_ASPEED)) == ("", "")
    assert countable_gpu(_inventory(_MATROX)) == ("", "")


def test_an_accelerator_beside_the_bmc_adapter_is_the_answer():
    vendor, _model = countable_gpu(_inventory(_ASPEED, _NVIDIA))

    assert "NVIDIA" in vendor


def test_the_order_the_devices_were_listed_in_does_not_matter():
    first = countable_gpu(_inventory(_NVIDIA, _ASPEED))
    second = countable_gpu(_inventory(_ASPEED, _NVIDIA))

    assert first == second


def test_no_gpus_listed_at_all_is_not_the_same_as_none_countable():
    # "The payload cannot answer" versus "the machine has none worth counting": the
    # first has to fall back to what was extracted, the second must not.
    assert countable_gpu({}) is None
    assert countable_gpu({"summary": {}}) is None
    assert countable_gpu({"summary": {"gpus": []}}) == ("", "")


def test_an_admin_rule_excludes_a_part_that_is_otherwise_an_accelerator():
    # An onboard iGPU is a real Intel device, so only a curated row can drop it.
    assert countable_gpu(_inventory(_INTEL_IGPU)) != ("", "")

    ComponentExclusionRule.objects.create(
        vendor_id="8086", device_id="4680", kind="gpu",
        reason="onboard iGPU, not the accelerator under test",
    )

    assert countable_gpu(_inventory(_INTEL_IGPU)) == ("", "")


# --- what the statistics then say ------------------------------------------------

def test_a_bmc_only_fleet_reports_no_gpu_vendors_at_all():
    for i in range(3):
        _sub(when=_at(2026, 9), identity_hash=f"h{i}",
             gpu_vendor="ASPEED", inventory=_inventory(_ASPEED))
    services.rebuild_survey_stats()

    sections = {s["dimension"]: s for s in stats.distribution("2026-09")}

    assert "gpu_vendor" not in sections, "an empty dimension is absent, not a zero row"


def test_shares_are_of_the_machines_that_have_a_gpu():
    # Four machines, one with an accelerator. The GPU dimension counts that one, and the
    # machine count for the period is still four.
    for i in range(3):
        _sub(when=_at(2026, 9), identity_hash=f"bmc{i}",
             gpu_vendor="ASPEED", inventory=_inventory(_ASPEED))
    _sub(when=_at(2026, 9), identity_hash="gpu",
         gpu_vendor="NVIDIA", inventory=_inventory(_ASPEED, _NVIDIA))
    services.rebuild_survey_stats()

    section = next(s for s in stats.distribution("2026-09")
                   if s["dimension"] == "gpu_vendor")

    assert section["total"] == 1
    assert round(section["buckets"][0].share, 1) == 100.0
    assert stats.machine_count("2026-09") == 4


def test_the_rollup_corrects_submissions_that_are_already_stored():
    """The reason this lives in the rollup: the rows are append-only, and a census that
    could only fix new submissions would carry the wrong answer for a year."""
    _sub(when=_at(2026, 9), identity_hash="old",
         gpu_vendor="ASPEED", gpu_model="ASPEED Graphics Family",
         inventory=_inventory(_ASPEED))
    services.rebuild_survey_stats()

    sections = {s["dimension"]: s for s in stats.distribution("2026-09")}
    stored = SurveySubmission.objects.get(identity_hash="old")

    assert "gpu_vendor" not in sections            # not counted
    assert stored.gpu_vendor == "ASPEED"           # but still recorded verbatim


def test_a_payload_with_no_gpu_list_keeps_what_was_extracted():
    # Nothing to apply a rule to, so the extracted column stands rather than the machine
    # being recorded as having no GPU.
    _sub(when=_at(2026, 9), identity_hash="old-payload", gpu_vendor="NVIDIA", inventory={})
    services.rebuild_survey_stats()

    section = next(s for s in stats.distribution("2026-09")
                   if s["dimension"] == "gpu_vendor")

    assert section["buckets"][0].label == "NVIDIA"


def test_the_vendor_is_named_the_way_the_platform_names_it():
    """pci.ids spells AMD as "Advanced Micro Devices, Inc. [AMD/ATI]", which is not a
    label anybody wants on a statistics page. The catalog already keeps the mapping, so
    the census reads that table rather than a second one that could disagree."""
    vendor, _ = countable_gpu(_inventory(_NVIDIA))
    assert vendor == "NVIDIA"

    amd = {"pci_ids": {"vendor": "Advanced Micro Devices, Inc. [AMD/ATI] [1002]",
                       "device": "Navi 31 [744a]"}, "driver": "amdgpu"}
    vendor, _ = countable_gpu(_inventory(amd))
    assert vendor == "AMD"


def test_an_unmapped_vendor_name_is_left_alone():
    """The alias table's own rule: a guessed mapping that is wrong merges two companies,
    which is worse than one long label.

    Tested on the mapping directly rather than through ``countable_gpu``, because a
    vendor with no alias cannot reach it: the categorical exclusion drops any display
    device whose vendor is not a known accelerator vendor, and every known one is in the
    table. See the note on that rule below.
    """
    from lumina.survey.devices import _vendor_name

    assert _vendor_name("Some New Accelerator Co.") == "Some New Accelerator Co."
    assert _vendor_name("NVIDIA Corporation") == "NVIDIA"
    assert _vendor_name("") == ""


def test_a_display_vendor_that_makes_no_accelerators_is_not_counted():
    """The categorical rule, stated here because the census now depends on it: a GPU
    vendor outside ``ACCELERATOR_VENDOR_IDS`` is treated as a management adapter. That is
    what removes ASPEED and Matrox without a row each, and it also means a genuinely new
    accelerator vendor is invisible to the statistics until it is added to that set.
    """
    newcomer = {"pci_ids": {"vendor": "Some New Accelerator Co. [abcd]",
                            "device": "X [0001]"}, "driver": "xpu"}

    assert countable_gpu(_inventory(newcomer)) == ("", "")
