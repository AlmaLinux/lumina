"""A collect run on a machine with a real GPU: from bundle to published statistics.

Every other test here exercises one link. This one runs the whole chain a person's
``alma-cert collect`` actually takes, with the inventory shape the collector really
writes, because that is where the links can disagree without any single test noticing:

    lspci -vmmnnk -> summary.gpus -> POST /api/v1/survey/ -> SurveySubmission
                  -> survey_rollup -> SurveyStat -> the statistics page

The payload below is verbatim from ``lspci -vmmnnk`` on a machine with an RTX 3090 and
the usual ASPEED BMC console, run through the suite's own GPU collector, so the ``pci_ids``
spellings are the real ones rather than a convenient invention. If the exclusion rule ever
starts dropping accelerators, or the vendor id stops parsing, this fails.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.urls import reverse

from lumina.accounts.models import ApiToken, TokenScope
from lumina.results.tests import factories as f
from lumina.survey import services, stats
from lumina.survey.models import SurveyStat, SurveySubmission

pytestmark = pytest.mark.django_db

# As the collector writes them: `lspci -vmmnnk` strings, ids included.
_NVIDIA = {
    "pci": "01:00.0",
    "pci_ids": {
        "vendor": "NVIDIA Corporation [10de]",
        "device": "GA102 [GeForce RTX 3090] [2204]",
        "subsystem_vendor": "ASUSTeK Computer Inc. [1043]",
        "subsystem_device": "GA102 [GeForce RTX 3090] [8756]",
    },
    "driver": "nvidia",
    "driver_version": "580.65.06",
    "runtime": {"cuda": "13.0"},
    "vbios": "95.02.3C.40.17",
}
_ASPEED = {
    "pci": "07:00.0",
    "pci_ids": {
        "vendor": "ASPEED Technology, Inc. [1a03]",
        "device": "ASPEED Graphics Family [2000]",
    },
    "driver": "ast",
    "driver_version": "kernel:5.14.0",
    "runtime": {},
    "vbios": None,
}


def _collect_report():
    """A collect-only report, as ``alma-cert collect`` writes one."""
    report = f.make_report(run_types=["collect"], results=[])
    report["inventory"]["summary"]["gpus"] = [_NVIDIA, _ASPEED]
    return report


def _token(user):
    token, raw = ApiToken.issue(
        user=user, name="survey", scopes=[TokenScope.submit.value], ttl_seconds=3600,
    )
    return raw


def test_a_collect_run_with_an_nvidia_card_reaches_the_statistics(client):
    user = User.objects.create_user("surveyor", password="pw")

    response = client.post(
        "/api/v1/survey/",
        {"bundle": f.as_upload(f.build_bundle(_collect_report()))},
        HTTP_AUTHORIZATION=f"Bearer {_token(user)}",
    )

    assert response.status_code in (200, 201), response.content

    submission = SurveySubmission.objects.get()
    assert submission.origin == SurveySubmission.ORIGIN_SURVEY
    # The raw record keeps every device the machine reported, BMC console included.
    assert len(submission.inventory["summary"]["gpus"]) == 2

    services.rebuild_survey_stats()

    period = stats.available_periods()["month"][0]
    sections = {s["dimension"]: s for s in stats.distribution(period)}

    assert "gpu_vendor" in sections, "the GPU dimension must exist for a machine with a GPU"
    assert [b.label for b in sections["gpu_vendor"]["buckets"]] == ["NVIDIA"]
    assert [b.label for b in sections["gpu_model"]["buckets"]] == ["GeForce RTX 3090"]


def test_the_bmc_console_is_not_what_gets_counted(client):
    user = User.objects.create_user("surveyor2", password="pw")
    client.post(
        "/api/v1/survey/",
        {"bundle": f.as_upload(f.build_bundle(_collect_report()))},
        HTTP_AUTHORIZATION=f"Bearer {_token(user)}",
    )
    services.rebuild_survey_stats()

    buckets = set(
        SurveyStat.objects.filter(dimension="gpu_vendor").values_list("bucket", flat=True)
    )

    assert buckets == {"NVIDIA"}
    assert "ASPEED" not in buckets


def test_the_page_shows_the_card(client):
    user = User.objects.create_user("surveyor3", password="pw")
    client.post(
        "/api/v1/survey/",
        {"bundle": f.as_upload(f.build_bundle(_collect_report()))},
        HTTP_AUTHORIZATION=f"Bearer {_token(user)}",
    )
    services.rebuild_survey_stats()

    body = client.get(reverse("results:stats")).content.decode()

    assert "GeForce RTX 3090" in body
    assert "NVIDIA" in body


# --- the divergence that produced a card on the page and none in the stats -------

# `lspci -vmmnnk` enumerates every device under `summary.pci_devices`. The GPU collector
# is a separate collector that then interrogates nvidia-smi and friends; `collect_all`
# catches its failures per collector, so a machine whose GPU collector errored writes an
# empty `summary["gpus"]` while the enumeration still carries the card.
_PCI_ENUMERATION = [
    {
        "pci": "01:00.0",
        "class_id": "0300",
        "class": "VGA compatible controller [0300]",
        "pci_ids": {
            "vendor": "NVIDIA Corporation [10de]",
            "device": "GA102 [GeForce RTX 3090] [2204]",
        },
        "driver": "nvidia",
    },
    {
        "pci": "07:00.0",
        "class_id": "0300",
        "class": "VGA compatible controller [0300]",
        "pci_ids": {
            "vendor": "ASPEED Technology, Inc. [1a03]",
            "device": "ASPEED Graphics Family [2000]",
        },
        "driver": "ast",
    },
    {
        "pci": "02:00.0",
        "class_id": "0200",
        "class": "Ethernet controller [0200]",
        "pci_ids": {"vendor": "Intel Corporation [8086]", "device": "I210 [1533]"},
        "driver": "igb",
    },
]


def test_a_card_the_review_page_shows_is_the_card_the_stats_count(client):
    """The reported bug, as an invariant.

    The submission's review page reads devices through ``categorized_devices``, which
    prefers the full PCI enumeration. The rollup read ``summary["gpus"]`` instead. On a
    machine whose GPU collector failed those two disagree, so the reviewer saw an NVIDIA
    card on the page while the statistics recorded no GPU at all. They now read the same
    categorizer, and this pins them together.
    """
    from lumina.survey.devices import device_view

    user = User.objects.create_user("diverged", password="pw")
    report = f.make_report(run_types=["collect"], results=[])
    report["inventory"]["summary"]["pci_devices"] = _PCI_ENUMERATION
    report["inventory"]["summary"]["gpus"] = []          # the collector that failed

    client.post(
        "/api/v1/survey/",
        {"bundle": f.as_upload(f.build_bundle(report))},
        HTTP_AUTHORIZATION=f"Bearer {_token(user)}",
    )
    submission = SurveySubmission.objects.get()

    # What a reviewer sees on the page.
    shown = [g["vendor"] for g in device_view(submission)["gpus"]]
    assert any("NVIDIA" in vendor for vendor in shown), shown

    services.rebuild_survey_stats()
    period = stats.available_periods()["month"][0]
    sections = {s["dimension"]: s for s in stats.distribution(period)}

    # And what the statistics count: the same card, with the BMC console still excluded.
    assert [b.label for b in sections["gpu_vendor"]["buckets"]] == ["NVIDIA"]


def test_a_machine_that_enumerates_only_a_bmc_console_counts_no_gpu(client):
    user = User.objects.create_user("bmconly", password="pw")
    report = f.make_report(run_types=["collect"], results=[])
    report["inventory"]["summary"]["pci_devices"] = [_PCI_ENUMERATION[1]]
    report["inventory"]["summary"]["gpus"] = []

    client.post(
        "/api/v1/survey/",
        {"bundle": f.as_upload(f.build_bundle(report))},
        HTTP_AUTHORIZATION=f"Bearer {_token(user)}",
    )
    services.rebuild_survey_stats()

    period = stats.available_periods()["month"][0]
    sections = {s["dimension"]: s for s in stats.distribution(period)}

    assert "gpu_vendor" not in sections
