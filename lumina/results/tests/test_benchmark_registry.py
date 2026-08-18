"""The benchmark registry is the authority on what is listed, ranked, and scored - not the data.

A benchmark exists for lumina only if the registry says so. Old rows for a retired benchmark, or a
benchmark id lumina was never taught, are ignored on every leaderboard surface rather than excluded
one place and forgotten in another.
"""
from __future__ import annotations

import pytest
from django.urls import reverse

from lumina.results import benchmark_registry as registry

pytestmark = pytest.mark.django_db


def test_active_set_excludes_retired_and_unknown():
    assert "bench.cpu.sysbench-single" in registry.active_ids()
    assert registry.is_listed("bench.cpu.sysbench-single")
    assert not registry.is_listed("bench.sched.hackbench"), "retired"
    assert not registry.is_listed("bench.made.up"), "never defined"


def test_axis_is_only_for_active_benchmarks():
    assert registry.axis_of("bench.cpu.sysbench-single") == registry.CPU_SINGLE
    assert registry.axis_of("bench.cpu.sysbench-multi") == registry.CPU_MULTI
    assert registry.axis_of("bench.gpu.clpeak") == registry.GPU
    assert registry.axis_of("bench.mem.bandwidth") is None, "listed, but not part of any Mark"
    assert registry.axis_of("bench.sched.hackbench") is None, "retired"
    assert registry.axis_of("bench.made.up") is None, "unknown"
    assert "bench.cpu.stressng-matrix" in registry.cpu_axis_ids()
    assert "bench.gpu.clpeak" in registry.gpu_axis_ids()


def test_label_keeps_retired_names_and_derives_unknown_ones():
    assert registry.label("bench.cpu.sysbench-single") == "CPU, single core"
    assert registry.label("bench.sched.hackbench") == "Scheduler, hackbench", "old rows still read"
    assert registry.label("bench.foo.bar-baz") == "Bar baz", "derived fallback"


def test_the_leaderboard_404s_for_an_unlisted_benchmark(client):
    for benchmark_id in ("bench.sched.hackbench", "bench.made.up"):
        resp = client.get(reverse("benchmarks:leaderboard", args=[benchmark_id]))
        assert resp.status_code == 404, benchmark_id


def test_a_defined_but_unrun_benchmark_shows_an_empty_page_not_a_404(client):
    resp = client.get(reverse("benchmarks:leaderboard", args=["bench.cpu.sysbench-single"]))

    assert resp.status_code == 200
    assert "No public results for this benchmark yet" in resp.content.decode()
