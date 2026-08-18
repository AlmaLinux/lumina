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


def test_a_gpu_share_is_of_the_whole_fleet_not_of_gpu_owners():
    """Four machines, one with an accelerator: "a quarter of machines have an NVIDIA GPU".

    The denominator is the machines in the period, not the machines that happen to have a
    countable GPU. Dividing by GPU owners would report NVIDIA at 100% on this fleet, which
    is true of a subset nobody asked about and would move whenever an unrelated machine
    gained or lost a card.
    """
    for i in range(3):
        _sub(when=_at(2026, 9), identity_hash=f"bmc{i}",
             gpu_vendor="ASPEED", inventory=_inventory(_ASPEED))
    _sub(when=_at(2026, 9), identity_hash="gpu",
         gpu_vendor="NVIDIA", inventory=_inventory(_ASPEED, _NVIDIA))
    services.rebuild_survey_stats()

    section = next(s for s in stats.distribution("2026-09")
                   if s["dimension"] == "gpu_vendor")

    assert stats.machine_count("2026-09") == 4
    assert section["total"] == 4
    assert [(b.label, round(b.share, 1)) for b in section["buckets"]] == [("NVIDIA", 25.0)]
    assert section["multi_valued"] is True


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


# --- a curated rule can hide a card, and the page has to say so ------------------

def test_a_reviewer_blacklisting_a_card_stops_it_being_counted():
    """The reviewer "blacklist device" action creates a rule naming that exact part. The
    census honours it, because a device not worth cataloguing is not worth counting:
    that is the rule you asked for. It is also the one way a working NVIDIA card can be
    invisible in the statistics while still being reported by the machine.
    """
    assert countable_gpu(_inventory(_NVIDIA)) != ("", "")

    ComponentExclusionRule.objects.create(
        vendor_id="10de", device_id="2230", kind="gpu",
        reason="excluded by a reviewer",
    )

    assert countable_gpu(_inventory(_NVIDIA)) == ("", "")


def test_a_kind_wide_rule_does_not_empty_the_whole_dimension():
    """A row with a kind and no vendor or device says "never auto-attach any GPU", which
    is a catalog switch about component creation. Read as "count no GPUs" it would delete
    the GPU statistics from one admin row, so the census ignores it. The categorical BMC
    rule still applies, because that one is about the part.
    """
    ComponentExclusionRule.objects.create(
        vendor_id="", device_id="", kind="gpu", reason="never auto-attach graphics",
    )

    assert countable_gpu(_inventory(_NVIDIA)) != ("", "")
    assert countable_gpu(_inventory(_ASPEED)) == ("", "")   # still a BMC console


def test_the_review_page_says_which_cards_are_counted():
    """The debugging cost this removes: the page listed every card the machine reported
    while the statistics counted a subset, with no way to tell which from the page."""
    from lumina.survey.devices import device_view

    sub = SurveySubmission.objects.create(
        origin=SurveySubmission.ORIGIN_SURVEY,
        trust_tier=SurveySubmission.TIER_VERIFIED,
        identity_hash="h",
        inventory={"summary": {"gpus": [_NVIDIA, _ASPEED]}},
    )

    by_vendor = {g["vendor"]: g for g in device_view(sub)["gpus"]}

    nvidia = next(g for v, g in by_vendor.items() if "NVIDIA" in v)
    aspeed = next(g for v, g in by_vendor.items() if "ASPEED" in v)
    assert nvidia["counted"] is True
    assert aspeed["counted"] is False
    assert "management display adapter" in aspeed["not_counted_because"]


def test_a_blacklisted_card_is_shown_as_not_counted_with_the_reviewers_reason():
    from lumina.survey.devices import device_view

    ComponentExclusionRule.objects.create(
        vendor_id="10de", device_id="2230", kind="gpu",
        reason="excluded by a reviewer",
    )
    sub = SurveySubmission.objects.create(
        origin=SurveySubmission.ORIGIN_SURVEY,
        trust_tier=SurveySubmission.TIER_VERIFIED,
        identity_hash="h", inventory={"summary": {"gpus": [_NVIDIA]}},
    )

    gpu = device_view(sub)["gpus"][0]

    assert gpu["counted"] is False
    assert gpu["not_counted_because"] == "excluded by a reviewer"


# --- a machine with more than one GPU --------------------------------------------

# Verbatim from the machine that found this: an Arrow Lake iGPU and a discrete RTX PRO
# 5000. Both are accelerator-vendor devices with a live driver, so "prefer the discrete
# one" could not tell them apart, and the machine was published as Intel.
_INTEL_ARROW_LAKE = {
    "pci": "00:02.0", "class_id": "0300",
    "pci_ids": {"vendor": "Intel Corporation [8086]",
                "device": "Arrow Lake-S [Intel Graphics] [7d67]"},
    "driver": "i915",
}
_RTX_PRO = {
    "pci": "01:00.0", "class_id": "0300",
    "pci_ids": {"vendor": "NVIDIA Corporation [10de]",
                "device": "NVIDIA RTX PRO 5000 Blackwell Generation Laptop GPU [2c38]"},
    "driver": "nvidia",
}


def test_a_machine_with_two_gpus_counts_under_both():
    """The bug this fixes: one GPU per machine meant the iGPU won and the RTX card was
    invisible. Both cards were listed as countable on the submission's own review page,
    which is what made it look like the statistics were ignoring the card."""
    from lumina.survey.devices import countable_gpus

    pairs = countable_gpus(_inventory(_INTEL_ARROW_LAKE, _RTX_PRO))

    assert [vendor for vendor, _ in pairs] == ["Intel", "NVIDIA"]


def test_both_reach_the_published_statistics():
    _sub(when=_at(2026, 9), identity_hash="dual",
         inventory=_inventory(_INTEL_ARROW_LAKE, _RTX_PRO))
    services.rebuild_survey_stats()

    vendors = next(s for s in stats.distribution("2026-09")
                   if s["dimension"] == "gpu_vendor")
    models = next(s for s in stats.distribution("2026-09")
                  if s["dimension"] == "gpu_model")

    assert sorted(b.label for b in vendors["buckets"]) == ["Intel", "NVIDIA"]
    assert "NVIDIA RTX PRO 5000 Blackwell Generation Laptop GPU" in [
        b.label for b in models["buckets"]
    ]
    # One machine, and it is 100% of the fleet for each of the two vendors it has.
    assert stats.machine_count("2026-09") == 1
    assert [round(b.share, 1) for b in vendors["buckets"]] == [100.0, 100.0]


def test_identical_cards_count_their_machine_once():
    # Four A100s in one box is one machine with NVIDIA, not four.
    card = dict(_RTX_PRO)
    _sub(when=_at(2026, 9), identity_hash="quad",
         inventory=_inventory(card, dict(card, pci="02:00.0"),
                              dict(card, pci="03:00.0"), dict(card, pci="04:00.0")))
    services.rebuild_survey_stats()

    section = next(s for s in stats.distribution("2026-09")
                   if s["dimension"] == "gpu_vendor")

    assert [(b.label, b.count) for b in section["buckets"]] == [("NVIDIA", 1)]


def test_an_excluded_card_beside_a_counted_one_is_still_excluded():
    _sub(when=_at(2026, 9), identity_hash="mixed",
         inventory=_inventory(_ASPEED, _INTEL_ARROW_LAKE, _RTX_PRO))
    services.rebuild_survey_stats()

    section = next(s for s in stats.distribution("2026-09")
                   if s["dimension"] == "gpu_vendor")

    assert sorted(b.label for b in section["buckets"]) == ["Intel", "NVIDIA"]
    assert "ASPEED" not in [b.label for b in section["buckets"]]


def test_a_reviewer_can_still_exclude_just_the_igpu():
    """Counting both is the default; a site that considers an onboard display irrelevant
    still has the rule mechanism, which is how the reported machine already excluded an
    AMD iGPU and a BMC console."""
    ComponentExclusionRule.objects.create(
        vendor_id="8086", device_id="7d67", kind="gpu",
        reason="onboard iGPU, not the accelerator under test",
    )
    from lumina.survey.devices import countable_gpus

    pairs = countable_gpus(_inventory(_INTEL_ARROW_LAKE, _RTX_PRO))

    assert [vendor for vendor, _ in pairs] == ["NVIDIA"]
