"""Socket count and DIMM topology: stored, filterable, and on the page.

Both were collected by the suite from the start - DMI type 4 socket counts and
type 17 memory devices since schema 1.0 - and neither was denormalized or
displayed, so nothing could filter or read them. An all-core benchmark score is
not comparable without the socket count: two sockets of the same part roughly
double the result.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone

from lumina.results import ingest
from lumina.results.inventory_extract import extract
from lumina.results.models import TestRun
from lumina.results.tests import factories as f

pytestmark = pytest.mark.django_db


@pytest.fixture
def run():
    submitter = User.objects.create_user("hw", email="hw@example.com")
    report = f.make_report(
        run_types=["benchmark"], run_id="dddddddd-0000-0000-0000-000000000001",
        results=[f.benchmark_result("bench.cpu.sysbench-multi", category="cpu")],
    )
    test_run = ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(f.build_bundle(report)),
        source="api",
    )
    test_run.status = TestRun.STATUS_APPROVED
    test_run.published_at = timezone.now()
    test_run.save(update_fields=["status", "published_at"])
    return test_run


# --- what ingest stores -------------------------------------------------------


def test_sockets_and_threads_are_denormalized(run):
    assert run.cpu_sockets == 2
    assert run.cpu_cores == 64
    assert run.cpu_threads == 128


def test_the_dimm_summary_is_denormalized(run):
    assert run.memory_dimm_count == 8
    assert run.memory_type == "DDR5"
    assert run.memory_speed_mts == 4800


def test_the_configured_speed_is_what_is_stored():
    """Modules rated 5600 that the platform clocks at 4800 ran at 4800.

    Storing the rated figure would overstate every machine that downclocks,
    which is most of them once more than two modules are installed.
    """
    inventory = f.default_inventory()
    values = extract(inventory)
    assert values["memory_speed_mts"] == 4800


def test_a_mixed_configuration_reports_the_slowest_module():
    """Mismatched modules all clock to the slowest, which is what ran."""
    inventory = f.default_inventory()
    dimms = inventory["summary"]["memory"]["dimms"]
    dimms[0] = {**dimms[0], "speed_mts": 3200}
    assert extract(inventory)["memory_speed_mts"] == 3200


def test_a_mixed_configuration_reports_the_commonest_type():
    inventory = f.default_inventory()
    dimms = inventory["summary"]["memory"]["dimms"]
    dimms[0] = {**dimms[0], "type": "DDR4"}
    assert extract(inventory)["memory_type"] == "DDR5"


def test_a_machine_with_no_dimm_detail_reports_nothing_rather_than_zero(run):
    """Older reports carry only a total. None says unknown; 0 would be a claim."""
    inventory = f.default_inventory()
    inventory["summary"]["memory"] = {"total_bytes": 8589934592, "dimms": []}
    values = extract(inventory)
    assert values["memory_dimm_count"] is None
    assert values["memory_type"] == ""
    assert values["memory_speed_mts"] is None
    assert values["memory_mb"] == 8192


def test_a_single_socket_machine_is_recorded_as_one_not_blank():
    inventory = f.default_inventory()
    inventory["summary"]["cpus"][0].update({"sockets": 1, "cores": 8, "threads": 16})
    values = extract(inventory)
    assert values["cpu_sockets"] == 1


# --- reading them -------------------------------------------------------------


def test_the_run_page_shows_sockets_threads_and_every_module(client, run):
    body = client.get(run.get_absolute_url()).content.decode()

    assert "2 sockets" in body
    assert "128 threads" in body
    # One row per populated module, with the detail that explains bandwidth.
    assert body.count("MTC40F2046S1RC56BD1") == 8
    assert "P0 CHANNEL A" in body or "DIMM 0" in body
    assert "DDR5" in body


def test_memory_is_shown_in_gigabytes(client, run):
    """"549755813888 bytes" and "524288 MB" are both numbers nobody converts."""
    body = client.get(run.get_absolute_url()).content.decode()
    assert "512 GB" in body
    assert "524288 MB" not in body


def test_the_dimm_helper_converts_sizes(run):
    assert [d["size_gb"] for d in run.dimms] == [64] * 8


# --- filtering ----------------------------------------------------------------


def test_sockets_are_a_leaderboard_facet(run):
    from lumina.results.filters import leaderboard_facets

    facets = leaderboard_facets("bench.cpu.sysbench-multi", None)

    assert facets["sockets"] == [2]
    assert facets["memory_type"] == ["DDR5"]
    assert facets["memory_speed"] == [4800]


def test_filtering_by_socket_count_selects_comparable_machines(run):
    from lumina.results.filters import filter_leaderboard

    two = filter_leaderboard(benchmark_id="bench.cpu.sysbench-multi",
                             params={"sockets": ["2"]})
    one = filter_leaderboard(benchmark_id="bench.cpu.sysbench-multi",
                             params={"sockets": ["1"]})

    assert list(two)
    assert not list(one)


def test_a_numeric_facet_does_not_break_the_facet_query(run):
    """exclude(field="") raises on an integer column, so the facet builder has
    to know which of its fields are numbers."""
    from lumina.results.filters import leaderboard_facets

    facets = leaderboard_facets("bench.cpu.sysbench-multi", None)
    assert set(facets) >= {"sockets", "memory_speed", "alma", "cpu"}


def test_the_leaderboard_marks_multi_socket_machines(client, run):
    body = client.get(
        reverse("benchmarks:leaderboard", args=["bench.cpu.sysbench-multi"])
        + "?group=none"
    ).content.decode()

    assert "2P" in body
    assert "64c/128t" in body
