"""Turning benchmark rows into readable ones.

The landing feed printed every primary metric of a run space-separated with its
raw identifier, so a full benchmark run rendered as one paragraph of seventeen
dotted strings: "bench.compile.python: 19.7 s bench.compress.xz: 168.2 MB/s
bench.compress.zstd: 6322 MB/s ...". The benchmarks index had the same problem
with a naive fix: it sliced the first three, and since Meta orders by
benchmark_id that meant a Python build time and two compression numbers, with no
CPU, memory, or disk figure on the page at all.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone

from lumina.results import ingest
from lumina.results.highlights import (
    attach_headlines,
    benchmark_label,
    format_metric,
    headline_metrics,
)
from lumina.results.models import BenchmarkResult, TestRun
from lumina.results.tests import factories as f

pytestmark = pytest.mark.django_db


# --- labels -------------------------------------------------------------------


def test_known_benchmarks_get_a_human_name():
    assert benchmark_label("bench.cpu.sysbench-multi") == "CPU, all cores"
    assert benchmark_label("bench.storage.fio-4k-randread") == "Disk random read"
    assert benchmark_label("bench.mem.latency") == "Memory latency"


def test_an_unmapped_benchmark_still_reads_as_words():
    """A benchmark added to the suite before this map should not break a page."""
    assert benchmark_label("bench.storage.fio-8k-mixed") == "Fio 8k mixed"
    assert benchmark_label("bench.new.thing_here") == "Thing here"


def test_an_empty_id_does_not_crash():
    assert benchmark_label("") == ""


# --- values -------------------------------------------------------------------


@pytest.mark.parametrize("value,expected", [
    (62869.1, "62,869"),        # grouped, and the tenth of an event is noise
    (132830.9, "132,831"),
    (52682.3, "52,682"),
    (6322, "6,322"),
    (468, "468"),
    (36.7, "36.7"),             # a tenth of a nanosecond is the measurement
    (19.7, "19.7"),
    (33, "33"),                 # not "33.0" - that claims precision it lost
    (0.1, "0.1"),
    (0, "0"),
])
def test_metric_formatting(value, expected):
    assert format_metric(value) == expected


def test_decimals_from_the_database_format_like_the_numbers_they_are():
    """The column is DecimalField(decimal_places=6), so a value arrives as
    41230.500000. Six places of stored precision is not six places of measured
    precision, and it is certainly not what a feed should print."""
    assert format_metric(Decimal("41230.500000")) == format_metric(41230.5)
    assert format_metric(Decimal("41230.500000")) == "41,230"
    assert format_metric(Decimal("36.700000")) == "36.7"


def test_a_negative_value_keeps_its_sign():
    assert format_metric(-12.5) == "-12.5"


# --- which metrics lead a row -------------------------------------------------


def _row(benchmark_id, category, *, primary=True, value=1):
    return BenchmarkResult(benchmark_id=benchmark_id, category=category,
                           metric="m", value=value, unit="u", is_primary=primary)


FULL_RUN = [
    _row("bench.compile.python", "compilation"),
    _row("bench.compress.xz", "compression"),
    _row("bench.compress.zstd", "compression"),
    _row("bench.cpu.stressng-matrix", "cpu"),
    _row("bench.cpu.sysbench-multi", "cpu"),
    _row("bench.cpu.sysbench-single", "cpu"),
    _row("bench.crypto.openssl-sha256", "crypto"),
    _row("bench.mem.bandwidth", "memory"),
    _row("bench.mem.latency", "memory"),
    _row("bench.sched.stressng-switch", "scheduler"),
]


def test_the_headline_three_are_the_chosen_ones_in_order():
    """Single core, all cores, memory bandwidth.

    Two CPU numbers on purpose: they are read as a pair, and one without the
    other says little about a machine. Alphabetically first in cpu is
    stressng-matrix, which is exactly why the choice is explicit rather than
    derived.
    """
    shown, _ = headline_metrics(FULL_RUN)
    assert [row.benchmark_id for row in shown] == [
        "bench.cpu.sysbench-single",
        "bench.cpu.sysbench-multi",
        "bench.mem.bandwidth",
    ]


def test_a_retired_benchmark_never_headlines_or_counts():
    """Storage benchmarks are retired; old rows still exist and must not lead - nor be counted
    among the metrics a run measured, since the registry no longer lists them at all."""
    rows = FULL_RUN + [_row("bench.storage.fio-4k-randread", "storage")]
    shown, remaining = headline_metrics(rows)
    assert not any("storage" in row.benchmark_id for row in shown)
    assert remaining == len(FULL_RUN) - 3, "the retired row is not counted either"


def test_a_run_without_the_chosen_three_still_leads_with_its_own_numbers():
    """A GPU-only run should show GPU numbers, not an empty row."""
    rows = [
        _row("bench.gpu.clpeak", "gpu"),
        _row("bench.gpu.cuda-bandwidth", "gpu"),
        _row("bench.net.iperf3-tcp", "network"),
    ]
    shown, remaining = headline_metrics(rows)
    assert [row.benchmark_id for row in shown] == [
        "bench.gpu.clpeak", "bench.net.iperf3-tcp",
    ]
    assert remaining == 1


def test_the_fallback_fills_around_a_partial_headline_set():
    """Memory bandwidth present, no sysbench: fill from other categories."""
    rows = [
        _row("bench.mem.bandwidth", "memory"),
        _row("bench.crypto.openssl-sha256", "crypto"),
        _row("bench.compress.zstd", "compression"),
    ]
    shown, _ = headline_metrics(rows)
    assert shown[0].benchmark_id == "bench.mem.bandwidth"
    assert len(shown) == 3
    # memory already spoke, so it does not get a second slot.
    assert [row.category for row in shown] == ["memory", "crypto", "compression"]


def test_no_metric_is_shown_twice():
    shown, _ = headline_metrics(FULL_RUN)
    assert len({id(row) for row in shown}) == len(shown)


def test_what_is_not_shown_is_counted():
    """A truncated row that says nothing reads as the whole story."""
    shown, remaining = headline_metrics(FULL_RUN)
    assert len(shown) == 3
    assert remaining == len(FULL_RUN) - 3


def test_non_primary_metrics_are_neither_shown_nor_counted():
    rows = FULL_RUN + [_row("bench.net.iperf3-tcp", "network", primary=False)]
    shown, remaining = headline_metrics(rows)
    assert all(row.is_primary for row in shown)
    assert remaining == len(FULL_RUN) - 3


def test_a_cpu_only_run_shows_just_its_one_number():
    shown, remaining = headline_metrics([_row("bench.cpu.sysbench-multi", "cpu")])
    assert len(shown) == 1
    assert remaining == 0


def test_a_category_falls_back_when_its_preferred_benchmark_is_absent():
    """crypto's preferred headline is AES; a run carrying only SHA still leads with SHA rather than
    nothing. (An unlisted id cannot be tested here - the registry filters it out before this.)"""
    shown, _ = headline_metrics([_row("bench.crypto.openssl-sha256", "crypto")])
    assert [row.benchmark_id for row in shown] == ["bench.crypto.openssl-sha256"]


def test_a_run_with_no_primary_metrics_yields_nothing():
    shown, remaining = headline_metrics([_row("bench.cpu.x", "cpu", primary=False)])
    assert shown == []
    assert remaining == 0


# --- the landing page ---------------------------------------------------------


def _published_benchmark_run(submitter):
    report = f.make_report(
        run_types=["benchmark"],
        run_id="ffffffff-0000-0000-0000-000000000001",
        results=[
            f.benchmark_result("bench.cpu.sysbench-multi", category="cpu"),
            f.benchmark_result("bench.mem.bandwidth", category="memory", metrics=[
                {"name": "bandwidth", "value": 52682.3, "unit": "MB/s",
                 "direction": "higher_is_better", "primary": True},
            ]),
            f.benchmark_result("bench.cpu.stressng-matrix", category="cpu", metrics=[
                {"name": "bogo_ops_per_sec", "value": 4821.0, "unit": "bogo-ops/s",
                 "direction": "higher_is_better", "primary": True},
            ]),
            f.benchmark_result("bench.compress.xz", category="compression",
                               metrics=[
                {"name": "throughput", "value": 168.2, "unit": "MB/s",
                 "direction": "higher_is_better", "primary": True},
            ]),
        ],
    )
    run = ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(f.build_bundle(report)),
        source="api",
    )
    run.status = TestRun.STATUS_APPROVED
    run.published_at = timezone.now()
    run.save(update_fields=["status", "published_at"])
    return run


@pytest.fixture
def bench_run():
    submitter = User.objects.create_user("hl", email="hl@example.com")
    return _published_benchmark_run(submitter)


def test_the_feed_names_benchmarks_instead_of_printing_identifiers(client, bench_run):
    body = client.get(reverse("core:home")).content.decode()

    assert "CPU, all cores" in body
    assert "Memory bandwidth" in body
    # The identifier belongs on the leaderboard, not in a feed paragraph.
    assert "bench.cpu.sysbench-multi" not in body
    assert "bench.compress.xz" not in body


def test_the_feed_formats_the_numbers(client, bench_run):
    body = client.get(reverse("core:home")).content.decode()
    assert "52,682" in body        # memory bandwidth, grouped
    assert "41,230" in body        # sysbench multi, rounded off its stored decimals
    assert "52682.300000" not in body


def test_the_feed_states_how_much_it_left_out(client, bench_run):
    body = client.get(reverse("core:home")).content.decode()
    assert "+1 more" in body


def test_resolving_corrected_kinds_does_not_grow_with_the_feed(client, bench_run):
    """display_name consults the alias table, so this could have been N+1.

    Asserting a constant rather than a number: what matters is that adding runs
    to the feed adds no queries.
    """
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    def alias_queries():
        with CaptureQueriesContext(connection) as captured:
            client.get(reverse("core:home"))
        return len([q for q in captured.captured_queries
                    if "reportedidentityalias" in q["sql"].lower()])

    one_run = alias_queries()
    submitter = User.objects.get(username="hl")
    for index in range(2, 5):
        report = f.make_report(
            run_types=["benchmark"],
            run_id=f"ffffffff-0000-0000-0000-00000000000{index}",
            results=[f.benchmark_result("bench.cpu.sysbench-multi", category="cpu")],
        )
        run = ingest.ingest_bundle(
            submitter=submitter, bundle_file=f.as_upload(f.build_bundle(report)),
            source="api",
        )
        run.status = TestRun.STATUS_APPROVED
        run.published_at = timezone.now()
        run.save(update_fields=["status", "published_at"])

    assert alias_queries() == one_run


def test_attach_headlines_annotates_without_touching_the_template(bench_run):
    runs = attach_headlines([bench_run])
    # This fixture has no single-core result, so the third slot falls back to the
    # next category that measured something.
    assert [row.display_label for row in runs[0].headlines] == [
        "CPU, all cores", "Memory bandwidth", "xz compression",
    ]
    assert runs[0].headlines_remaining == 1


# --- the feeds cost a constant number of queries ------------------------------


def _published_validation(submitter, index, *, failing=False):
    report = f.make_report(
        run_types=["validate"],
        run_id=f"eeeeeeee-0000-0000-0000-00000000000{index}",
        results=[
            f.validate_result("validate.cpu.functional"),
            f.validate_result("validate.memory.functional",
                              status="fail" if failing else "pass"),
        ],
    )
    run = ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(f.build_bundle(report)),
        source="api",
    )
    run.status = TestRun.STATUS_APPROVED
    run.published_at = timezone.now()
    run.save(update_fields=["status", "published_at"])
    return run


def _home_query_tables(client):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    with CaptureQueriesContext(connection) as captured:
        client.get(reverse("core:home"))
    return [q["sql"].lower() for q in captured.captured_queries]


def test_the_pass_badge_does_not_cost_a_query_per_run(client):
    """verdict() ran an EXISTS per row, purely to choose PASS or FAIL.

    Five validation runs on the landing page meant five extra queries, growing
    with the feed. The view prefetches results and verdict() honors that cache.
    """
    submitter = User.objects.create_user("qc", email="qc@example.com")
    _published_validation(submitter, 1)
    one = len([sql for sql in _home_query_tables(client)
               if "results_testresult" in sql])

    for index in range(2, 6):
        _published_validation(submitter, index)
    many = len([sql for sql in _home_query_tables(client)
                if "results_testresult" in sql])

    assert one == many == 1


def test_the_verdict_is_still_right_when_it_comes_from_the_prefetch(client):
    """The cheap path has to reach the same conclusion as the query."""
    submitter = User.objects.create_user("qv", email="qv@example.com")
    passing = _published_validation(submitter, 7)
    failing = _published_validation(submitter, 8, failing=True)

    assert passing.verdict() is True
    assert failing.verdict() is False

    prefetched = {run.pk: run for run in
                  TestRun.objects.filter(pk__in=[passing.pk, failing.pk])
                  .prefetch_related("results")}
    assert prefetched[passing.pk].verdict() is True
    assert prefetched[failing.pk].verdict() is False

    body = client.get(reverse("core:home")).content.decode()
    assert "PASS" in body and "FAIL" in body


def test_informational_failures_still_do_not_gate_via_the_prefetch():
    submitter = User.objects.create_user("qi", email="qi@example.com")
    run = _published_validation(submitter, 9)
    run.results.create(test_id="validate.gpu.driver", status="fail",
                       severity="informational", category="gpu")

    fresh = TestRun.objects.prefetch_related("results").get(pk=run.pk)

    assert fresh.verdict() is True


# --- combined runs carry benchmarks without being filed as one ----------------


def _combined_run(submitter, index):
    """What `alma-cert run` produces: collect, validate, and benchmark together."""
    report = f.make_report(
        run_types=["collect", "validate", "benchmark"],
        run_id=f"cccccccc-0000-0000-0000-00000000000{index}",
        results=[
            f.validate_result("validate.cpu.functional"),
            f.benchmark_result("bench.cpu.sysbench-multi", category="cpu"),
        ],
    )
    run = ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(f.build_bundle(report)),
        source="api",
    )
    run.status = TestRun.STATUS_APPROVED
    run.published_at = timezone.now()
    run.save(update_fields=["status", "published_at"])
    return run


def test_the_benchmark_feed_finds_runs_by_what_they_carry(client):
    """Three runs with metrics showed as one, because two were combined runs.

    Filtering run_type="benchmark" asks how a run was filed, and a combined run
    is filed as validate. Its metrics were in the database the whole time.
    """
    submitter = User.objects.create_user("cf", email="cf@example.com")
    _published_benchmark_run(submitter)      # filed as benchmark
    _combined_run(submitter, 2)              # filed as validate
    _combined_run(submitter, 3)              # filed as validate

    assert TestRun.objects.public().filter(run_type="benchmark").count() == 1
    assert TestRun.objects.public().with_benchmarks().count() == 3

    body = client.get(reverse("core:home")).content.decode()
    assert "+1 more" in body
    from lumina.results.highlights import attach_headlines
    from lumina.results.services import apply_alias_kinds
    shown = attach_headlines(apply_alias_kinds(
        TestRun.objects.public().with_benchmarks().prefetch_related("benchmarks")))
    assert len(shown) == 3


def test_a_run_without_benchmarks_is_not_in_the_benchmark_feed():
    """with_benchmarks asks for metrics, not for a label, in both directions."""
    submitter = User.objects.create_user("co", email="co@example.com")
    report = f.make_report(
        run_types=["validate"], run_id="cccccccc-0000-0000-0000-000000000099",
        results=[],
    )
    run = ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(f.build_bundle(report)),
        source="api",
    )
    run.status = TestRun.STATUS_APPROVED
    run.published_at = timezone.now()
    run.save(update_fields=["status", "published_at"])

    assert not TestRun.objects.with_benchmarks().filter(pk=run.pk).exists()


def test_with_benchmarks_returns_each_run_once():
    """A join against many metric rows would repeat the run per metric."""
    submitter = User.objects.create_user("cd", email="cd@example.com")
    run = _published_benchmark_run(submitter)
    assert run.benchmarks.count() > 1

    found = list(TestRun.objects.public().with_benchmarks())

    assert found.count(run) == 1
    assert len(found) == 1


def test_the_benchmark_atom_feed_also_finds_combined_runs(client):
    submitter = User.objects.create_user("ca", email="ca@example.com")
    _combined_run(submitter, 4)

    body = client.get(reverse("results:benchmarks_feed")).content.decode()

    assert "Benchmarks:" in body
