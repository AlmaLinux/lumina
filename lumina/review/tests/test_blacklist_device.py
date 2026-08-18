"""The reviewer's inline "blacklist this model" action on a run's detail page.

A reviewer who never wants to see a part again (an onboard iGPU, a management NIC) creates a
permanent ``ComponentExclusionRule`` straight from the review screen rather than hand-typing PCI
ids into the admin. The rule is global - it unticks that model by default on every future run - so
it is audit-logged and the current run is re-seeded at once so the effect is visible immediately.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.urls import reverse

from lumina.audit.models import AuditLogEntry
from lumina.hardware.models import ComponentExclusionRule, ComponentKind
from lumina.results import ingest, services
from lumina.results.models import TestRun
from lumina.results.tests import factories as f

pytestmark = pytest.mark.django_db
User = get_user_model()

NVIDIA = {"pci": "81:00.0", "class": "VGA compatible controller [0300]", "class_id": "0300",
          "pci_ids": {"vendor": "NVIDIA Corporation [10de]",
                      "device": "AD102GL [L40S] [26b9]"}, "driver": "nvidia"}
MLX = {"pci": "01:00.0", "class": "Ethernet controller [0200]", "class_id": "0200",
       "pci_ids": {"vendor": "Mellanox Technologies [15b3]",
                   "device": "MT27710 Family [ConnectX-4 Lx] [1015]"}, "driver": "mlx5_core"}


@pytest.fixture
def reviewer(client):
    u = User.objects.create_user(username="rev")
    u.groups.add(Group.objects.create(name="reviewer"))
    client.force_login(u)
    return u


@pytest.fixture
def plain(client):
    u = User.objects.create_user(username="plain")
    client.force_login(u)
    return u


@pytest.fixture
def submitter():
    return User.objects.create_user(username="sub")


def _run(pci_devices, submitter) -> TestRun:
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


def _url(run):
    return reverse("review:run_blacklist_device", args=[run.pk])


class BlacklistDeviceTests:
    def test_creates_rule_logs_and_excludes_on_this_run(self, client, reviewer, submitter):
        run = _run([NVIDIA], submitter)
        # Precondition: the accelerator is attached (ticked) before any rule exists.
        assert _by_model(run)["AD102GL [L40S]"]["excluded"] is False

        resp = client.post(_url(run), {
            "vendor_id": "10de", "device_id": "26b9", "kind": ComponentKind.gpu.value,
            "reason": "test rig card, not for the catalog",
        })
        assert resp.status_code == 302

        rule = ComponentExclusionRule.objects.get(vendor_id="10de", device_id="26b9")
        assert rule.kind == ComponentKind.gpu.value
        assert rule.reason == "test rig card, not for the catalog"
        assert AuditLogEntry.objects.filter(action="component_exclusion.create").count() == 1
        # Re-seeded immediately: the device is now unticked by default on this very run.
        run.refresh_from_db()
        excluded = _by_model(run)["AD102GL [L40S]"]
        assert excluded["excluded"] is True
        assert excluded["excluded_reason"] == "test rig card, not for the catalog"

    def test_ids_are_stored_lowercase(self, client, reviewer, submitter):
        run = _run([MLX], submitter)
        client.post(_url(run), {
            "vendor_id": "15B3", "device_id": "1015", "kind": ComponentKind.nic.value,
            "reason": "lab NIC",
        })
        assert ComponentExclusionRule.objects.filter(vendor_id="15b3", device_id="1015").exists()

    def test_blank_reason_gets_a_default(self, client, reviewer, submitter):
        run = _run([MLX], submitter)
        client.post(_url(run), {
            "vendor_id": "15b3", "device_id": "1015", "kind": ComponentKind.nic.value,
            "reason": "   ",
        })
        rule = ComponentExclusionRule.objects.get(vendor_id="15b3", device_id="1015")
        assert rule.reason

    def test_second_blacklist_is_idempotent(self, client, reviewer, submitter):
        run = _run([NVIDIA], submitter)
        post = lambda: client.post(_url(run), {  # noqa: E731
            "vendor_id": "10de", "device_id": "26b9", "kind": ComponentKind.gpu.value,
            "reason": "dupe",
        })
        post()
        post()
        assert ComponentExclusionRule.objects.filter(vendor_id="10de", device_id="26b9").count() == 1
        # The second press is not a second rule, so it is not a second audit entry either.
        assert AuditLogEntry.objects.filter(action="component_exclusion.create").count() == 1

    def test_missing_pci_ids_creates_no_rule(self, client, reviewer, submitter):
        run = _run([NVIDIA], submitter)
        resp = client.post(_url(run), {"vendor_id": "", "device_id": "", "reason": "x"})
        assert resp.status_code == 302
        assert not ComponentExclusionRule.objects.exists()

    def test_unknown_kind_is_dropped_to_any(self, client, reviewer, submitter):
        run = _run([NVIDIA], submitter)
        client.post(_url(run), {
            "vendor_id": "10de", "device_id": "26b9", "kind": "not-a-kind", "reason": "x",
        })
        rule = ComponentExclusionRule.objects.get(vendor_id="10de", device_id="26b9")
        assert rule.kind == ""

    def test_non_reviewer_cannot_blacklist(self, client, plain, submitter):
        run = _run([NVIDIA], submitter)
        resp = client.post(_url(run), {
            "vendor_id": "10de", "device_id": "26b9", "kind": ComponentKind.gpu.value,
            "reason": "x",
        })
        assert resp.status_code in (302, 403)
        assert not ComponentExclusionRule.objects.exists()

    def test_get_is_not_allowed(self, client, reviewer, submitter):
        run = _run([NVIDIA], submitter)
        assert client.get(_url(run)).status_code == 405


class BlacklistControlRenderingTests:
    def test_control_shows_for_a_pci_device(self, client, reviewer, submitter):
        run = _run([NVIDIA], submitter)
        html = client.get(reverse("review:run_detail", args=[run.pk])).content.decode()
        assert "Blacklist this model" in html
        assert f'action="{_url(run)}"' in html

    def test_no_control_without_a_pci_id(self, client, reviewer, submitter):
        # The manually-attached CPU family has no PCI id, so it is not blacklistable.
        run = _run([], submitter)
        entries = services.preview_component_ties(run)
        assert entries  # a CPU row at least
        assert all(not e.get("blacklistable") for e in entries)
