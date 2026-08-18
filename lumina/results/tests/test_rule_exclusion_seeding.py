"""Exclusion rules unticking devices by default at ingest, overridable in review.

A BMC display adapter and an onboard iGPU are things the catalog never wants; a rule (categorical
for BMC display, curated for specific parts) unticks them by default and records why, and a reviewer
can tick one to include it. Devices no rule matches are unaffected.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User

from lumina.hardware.models import ComponentExclusionRule, ComponentKind
from lumina.results import ingest, services
from lumina.results.exclusions import MANAGEMENT_DISPLAY_REASON
from lumina.results.models import TestRun
from lumina.results.tests import factories as f

pytestmark = pytest.mark.django_db

ASPEED = {"pci": "0c:00.0", "class": "VGA compatible controller [0300]", "class_id": "0300",
          "pci_ids": {"vendor": "ASPEED Technology, Inc. [1a03]",
                      "device": "ASPEED Graphics Family [2000]"}, "driver": "ast"}
NVIDIA = {"pci": "81:00.0", "class": "VGA compatible controller [0300]", "class_id": "0300",
          "pci_ids": {"vendor": "NVIDIA Corporation [10de]",
                      "device": "AD102GL [L40S] [26b9]"}, "driver": "nvidia"}
MLX = {"pci": "01:00.0", "class": "Ethernet controller [0200]", "class_id": "0200",
       "pci_ids": {"vendor": "Mellanox Technologies [15b3]",
                   "device": "MT27710 Family [ConnectX-4 Lx] [1015]",
                   "subsystem_device": "Device [0065]"}, "driver": "mlx5_core"}


@pytest.fixture
def submitter():
    return User.objects.create_user("sub", email="s@example.com")


def _ingest(pci_devices, submitter):
    inv = f.default_inventory()
    inv["summary"]["gpus"] = []
    inv["summary"]["nics"] = []
    inv["summary"]["pci_devices"] = pci_devices
    run = ingest.ingest_bundle(
        submitter=submitter, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"], inventory=inv,
            results=[f.validate_result("validate.cpu.functional")],
        ))),
    )
    return TestRun.objects.get(pk=run.pk)


def _by_model(run):
    return {e["raw_model"]: e for e in services.preview_component_ties(run)}


def test_a_bmc_display_is_unticked_by_the_categorical_rule(submitter):
    run = _ingest([ASPEED, NVIDIA], submitter)

    rows = _by_model(run)
    aspeed = rows["ASPEED Graphics Family"]
    assert aspeed["excluded"] is True
    assert aspeed["excluded_reason"] == MANAGEMENT_DISPLAY_REASON
    # The real accelerator beside it is untouched.
    nvidia = rows["AD102GL [L40S]"]
    assert nvidia["excluded"] is False
    assert nvidia["excluded_reason"] is None


def test_a_curated_rule_unticks_a_specific_part(submitter):
    ComponentExclusionRule.objects.create(
        vendor_id="15b3", kind=ComponentKind.nic.value, reason="lab NIC, not for the catalog",
    )
    run = _ingest([MLX, NVIDIA], submitter)

    rows = _by_model(run)
    mlx = rows["MT27710 Family [ConnectX-4 Lx]"]
    assert mlx["excluded"] is True
    assert mlx["excluded_reason"] == "lab NIC, not for the catalog"
    assert rows["AD102GL [L40S]"]["excluded"] is False


def test_the_seed_lands_in_the_run_fields(submitter):
    run = _ingest([ASPEED], submitter)
    aspeed_key = services.tie_key(ComponentKind.gpu, "ASPEED Graphics Family")
    assert aspeed_key in run.excluded_component_ties
    assert run.component_exclusion_reasons[aspeed_key] == MANAGEMENT_DISPLAY_REASON


def test_a_reviewer_can_include_a_rule_excluded_device(submitter):
    """Ticking it to include removes it from the excluded set (the human decision the save writes);
    the reason then stops showing, because a reason is only surfaced while a key is excluded."""
    run = _ingest([ASPEED], submitter)
    # Simulate the reviewer including it (what the form's save does with excluded_tie_keys()).
    run.excluded_component_ties = []
    run.save(update_fields=["excluded_component_ties"])

    aspeed = _by_model(run)["ASPEED Graphics Family"]
    assert aspeed["excluded"] is False
    assert aspeed["excluded_reason"] is None


def test_no_rules_no_exclusions(submitter):
    run = _ingest([NVIDIA, MLX], submitter)
    assert run.excluded_component_ties == []
    assert run.component_exclusion_reasons == {}
