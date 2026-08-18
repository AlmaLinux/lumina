"""Composite benchmark Marks: the relative scores an overall leaderboard ranks on.

The design (see ``benchmark_scoring``): each metric is divided by a reference - the geometric mean of
the current public results for it, computed live - and the geomean of those ratios, times 1000, is
the Mark. So a machine at the reference scores 1000, a machine twice as fast scores twice as much,
and because the reference is the *current* field, a machine's Mark shifts as the field grows. These
are the properties pinned here; exact numbers would be brittle but ratios are the whole point.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import Group, User

from lumina.results import benchmark_scoring, ingest, services
from lumina.results.tests import factories as f
from lumina.results.tests.helpers import release

pytestmark = pytest.mark.django_db

SINGLE = "bench.cpu.sysbench-single"
MULTI = "bench.cpu.sysbench-multi"


@pytest.fixture
def submitter():
    return User.objects.create_user("scorer")


@pytest.fixture
def reviewer():
    user = User.objects.create_user("score-rev")
    group, _ = Group.objects.get_or_create(name="reviewer")
    user.groups.add(group)
    return user


def _run(submitter, reviewer, *, cpu, run_id, benchmarks, sockets=None):
    """A public benchmark run for ``cpu``. ``benchmarks``: ``(benchmark_id, value)`` on events/s."""
    inventory = f.default_inventory()
    inventory["summary"]["cpus"][0]["model"] = cpu
    if sockets is not None:
        inventory["summary"]["cpus"][0]["sockets"] = sockets
    report = f.make_report(
        run_types=["benchmark"], run_id=run_id, inventory=inventory,
        results=[
            f.benchmark_result(
                test_id=bid, category="cpu",
                metrics=[{"name": "events_per_sec", "value": value, "unit": "events/s",
                          "direction": "higher_is_better", "primary": True}],
            )
            for bid, value in benchmarks
        ],
    )
    run = ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(f.build_bundle(report)), source="api",
    )
    services.approve_run(release(run), by=reviewer)
    return run


def _by_model(board):
    return {entry["model"]: entry for entry in board}


def test_the_only_machine_in_the_field_scores_1000(submitter, reviewer):
    """The field is itself, so it sits exactly at the reference on every axis."""
    _run(submitter, reviewer, cpu="EPYC 9354", run_id="11111111-0000-0000-0000-000000000001",
         benchmarks=[(SINGLE, 100), (MULTI, 1000)])

    board = benchmark_scoring.cpu_leaderboard()

    assert len(board) == 1
    entry = board[0]
    assert entry["single"] == 1000
    assert entry["multi"] == 1000
    assert entry["overall"] == 1000
    assert entry["runs"] == 1


def test_twice_as_fast_scores_twice_as_much_and_ranks_first(submitter, reviewer):
    _run(submitter, reviewer, cpu="Slow", run_id="22222222-0000-0000-0000-000000000001",
         benchmarks=[(SINGLE, 100), (MULTI, 1000)])
    _run(submitter, reviewer, cpu="Fast", run_id="22222222-0000-0000-0000-000000000002",
         benchmarks=[(SINGLE, 200), (MULTI, 2000)])

    board = benchmark_scoring.cpu_leaderboard()

    assert board[0]["model"] == "Fast", "sorted by overall, descending"
    marks = _by_model(board)
    # Fast is 2x Slow on every metric, and a geomean of ratios preserves that.
    assert abs(marks["Fast"]["overall"] - 2 * marks["Slow"]["overall"]) <= 2


def test_single_and_multi_are_independent_axes(submitter, reviewer):
    """Same single-core, one much stronger multi-core: only the multi Mark should move."""
    _run(submitter, reviewer, cpu="A", run_id="33333333-0000-0000-0000-000000000001",
         benchmarks=[(SINGLE, 100), (MULTI, 1000)])
    _run(submitter, reviewer, cpu="B", run_id="33333333-0000-0000-0000-000000000002",
         benchmarks=[(SINGLE, 100), (MULTI, 4000)])

    marks = _by_model(benchmark_scoring.cpu_leaderboard())

    assert marks["A"]["single"] == marks["B"]["single"], "single-core is unchanged"
    assert marks["B"]["multi"] > marks["A"]["multi"]


def test_the_median_across_runs_sets_a_models_mark(submitter, reviewer):
    """Two runs of one CPU, so its metric value is their median, not the latest or the best."""
    for i, value in enumerate((100, 300), start=1):
        _run(submitter, reviewer, cpu="EPYC 9354",
             run_id=f"44444444-0000-0000-0000-00000000000{i}",
             benchmarks=[(SINGLE, value), (MULTI, value * 10)])

    board = benchmark_scoring.cpu_leaderboard()

    assert board[0]["runs"] == 2
    # reference = geomean(100, 300) = 173.2; the model's own median is 200, so 1000 * 200/173.2.
    assert board[0]["single"] == round(1000 * 200 / (100 * 300) ** 0.5)


def test_a_marks_are_relative_so_they_shift_as_the_field_grows(submitter, reviewer):
    """The point of a relative rating: adding a much faster machine raises the reference, so an
    existing machine's Mark drops even though its own numbers never changed."""
    _run(submitter, reviewer, cpu="A", run_id="55555555-0000-0000-0000-000000000001",
         benchmarks=[(SINGLE, 100), (MULTI, 1000)])
    before = _by_model(benchmark_scoring.cpu_leaderboard())["A"]["overall"]

    _run(submitter, reviewer, cpu="B", run_id="55555555-0000-0000-0000-000000000002",
         benchmarks=[(SINGLE, 10000), (MULTI, 100000)])
    after = _by_model(benchmark_scoring.cpu_leaderboard())["A"]["overall"]

    assert after < before


def test_the_gpu_leaderboard_ranks_cards_by_mark(submitter, reviewer):
    one = _run(submitter, reviewer, cpu="host", run_id="77777777-0000-0000-0000-000000000001",
               benchmarks=[(SINGLE, 100)])
    two = _run(submitter, reviewer, cpu="host", run_id="77777777-0000-0000-0000-000000000002",
               benchmarks=[(SINGLE, 100)])
    for run, gflops in ((one, 10000), (two, 40000)):
        run.benchmarks.create(
            benchmark_id="bench.gpu.clpeak", benchmark_version="1",
            metric="single_precision_compute", value=gflops, unit="GFLOPS",
            direction="higher_is_better", category="gpu", is_primary=True,
            device_model="GeForce RTX 4090", device_pci_id="10de:2684",
        )

    board = benchmark_scoring.gpu_leaderboard()

    assert len(board) == 1, "both runs are the same card, so one grouped row"
    assert board[0]["model"] == "GeForce RTX 4090"
    assert board[0]["runs"] == 2
    # reference = geomean(10000, 40000) = 20000; card median = 25000, so 1000 * 25000/20000.
    assert board[0]["overall"] == round(1000 * 25000 / (10000 * 40000) ** 0.5)


def test_the_same_cpu_at_different_socket_counts_is_two_rows(submitter, reviewer):
    """Two sockets of one part roughly double the multi-core result, so they rank as different
    machines rather than being averaged into a single row."""
    _run(submitter, reviewer, cpu="EPYC 7302P", sockets=1,
         run_id="99999999-0000-0000-0000-000000000001", benchmarks=[(SINGLE, 100), (MULTI, 1000)])
    _run(submitter, reviewer, cpu="EPYC 7302P", sockets=2,
         run_id="99999999-0000-0000-0000-000000000002", benchmarks=[(SINGLE, 100), (MULTI, 2000)])

    board = benchmark_scoring.cpu_leaderboard()

    assert len(board) == 2, "same CPU, different socket counts -> two rows"
    by_sockets = {entry["sockets"]: entry for entry in board}
    assert by_sockets[1]["single"] == by_sockets[2]["single"], "single-core is per-core, unchanged"
    assert by_sockets[2]["multi"] > by_sockets[1]["multi"], "the dual-socket board scores higher"


def test_a_card_with_a_pci_id_on_only_some_benchmarks_is_one_row(submitter, reviewer):
    """Reported: a GPU showed twice. Its clpeak results carried a PCI id and its cuda-bandwidth ones
    did not, so it split into a PCI-keyed row and an identical model-keyed twin. One card, one row."""
    run = _run(submitter, reviewer, cpu="host", run_id="bbbbbbbb-0000-0000-0000-000000000001",
               benchmarks=[(SINGLE, 100)])
    run.benchmarks.create(
        benchmark_id="bench.gpu.clpeak", benchmark_version="1",
        metric="single_precision_compute", value=10000, unit="GFLOPS",
        direction="higher_is_better", category="gpu", is_primary=True,
        device_model="GeForce RTX 4090", device_pci_id="10de:2684",
    )
    run.benchmarks.create(
        benchmark_id="bench.gpu.cuda-bandwidth", benchmark_version="1",
        metric="device_to_device", value=900, unit="GB/s",
        direction="higher_is_better", category="gpu",
        device_model="GeForce RTX 4090", device_pci_id="",  # blank on this benchmark
    )

    board = benchmark_scoring.gpu_leaderboard()

    assert len(board) == 1, "the same card, not two"
    assert board[0]["model"] == "GeForce RTX 4090"


def test_the_scores_page_renders_sorts_and_switches_kind(client, submitter, reviewer):
    from django.urls import reverse

    _run(submitter, reviewer, cpu="SlowCpu", run_id="aaaaaaaa-0000-0000-0000-000000000001",
         benchmarks=[(SINGLE, 100), (MULTI, 1000)])
    _run(submitter, reviewer, cpu="FastCpu", run_id="aaaaaaaa-0000-0000-0000-000000000002",
         benchmarks=[(SINGLE, 400), (MULTI, 4000)])
    url = reverse("benchmarks:scores")

    default = client.get(url).content.decode()
    assert "Overall scores" in default
    assert default.index("FastCpu") < default.index("SlowCpu"), "overall, descending, by default"

    ascending = client.get(url, {"sort": "overall", "dir": "asc"}).content.decode()
    assert ascending.index("SlowCpu") < ascending.index("FastCpu"), "sorted ascending on request"

    gpu = client.get(url, {"kind": "gpu"}).content.decode()
    assert "GPUs" in gpu, "the GPU board is its own tab"
