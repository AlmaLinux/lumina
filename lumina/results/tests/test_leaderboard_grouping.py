"""Grouped leaderboards: aggregate by hardware, drill down to runs."""
from __future__ import annotations

import pytest
from django.contrib.auth.models import Group, User
from django.urls import reverse

from lumina.results import filters, ingest, services
from lumina.results.tests import factories as f
from lumina.results.tests.helpers import release

pytestmark = pytest.mark.django_db


@pytest.fixture
def submitter():
    return User.objects.create_user("bencher")


@pytest.fixture
def reviewer():
    user = User.objects.create_user("rev2")
    group, _ = Group.objects.get_or_create(name="reviewer")
    user.groups.add(group)
    return user


def _bench_run(submitter, reviewer, *, cpu, value, run_id, unit="events/s",
               direction="higher_is_better", benchmark="bench.cpu.sysbench-multi",
               category=None):
    inventory = f.default_inventory()
    inventory["summary"]["cpus"][0]["model"] = cpu
    report = f.make_report(
        run_types=["benchmark"],
        run_id=run_id,
        inventory=inventory,
        results=[f.benchmark_result(
            test_id=benchmark,
            # The factory defaults every category to "cpu", so a test about a GPU benchmark files
            # its result under the CPU section unless it says otherwise. Derived from the id here,
            # which is what the suite itself reports.
            category=category or benchmark.split(".")[1],
            metrics=[{"name": "events_per_sec", "value": value, "unit": unit,
                      "direction": direction, "primary": True}],
        )],
    )
    run = ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(f.build_bundle(report)),
        source="api",
    )
    services.approve_run(release(run), by=reviewer)
    return run


BENCH = "bench.cpu.sysbench-multi"


def test_groups_aggregate_by_cpu_with_median_best_and_count(submitter, reviewer):
    # two runs of the same CPU, one of another
    _bench_run(submitter, reviewer, cpu="EPYC 9354", value=100,
               run_id="11111111-0000-0000-0000-000000000001")
    _bench_run(submitter, reviewer, cpu="EPYC 9354", value=300,
               run_id="11111111-0000-0000-0000-000000000002")
    _bench_run(submitter, reviewer, cpu="Xeon 6430", value=150,
               run_id="11111111-0000-0000-0000-000000000003")

    data = filters.leaderboard_groups(benchmark_id=BENCH, group_by="cpu")

    assert data["group_by"] == "cpu"
    assert [g["key"] for g in data["groups"]] == ["EPYC 9354", "Xeon 6430"]
    epyc = data["groups"][0]
    assert epyc["median"] == 200          # (100 + 300) / 2
    assert epyc["best"] == 300            # higher-is-better
    assert epyc["runs"] == 2
    assert epyc["rank"] == 1
    assert epyc["percent"] == 100.0       # the ceiling row fills the track
    assert data["groups"][1]["percent"] == 75.0   # 150 / 200


def test_lower_is_better_ranks_ascending(submitter, reviewer):
    bench = "bench.compile.python"
    _bench_run(submitter, reviewer, cpu="Fast CPU", value=50, unit="s",
               direction="lower_is_better", benchmark=bench,
               run_id="22222222-0000-0000-0000-000000000001")
    _bench_run(submitter, reviewer, cpu="Slow CPU", value=200, unit="s",
               direction="lower_is_better", benchmark=bench,
               run_id="22222222-0000-0000-0000-000000000002")

    data = filters.leaderboard_groups(benchmark_id=bench)

    assert data["lower_better"] is True
    assert [g["key"] for g in data["groups"]] == ["Fast CPU", "Slow CPU"]
    assert data["groups"][0]["best"] == 50   # best = minimum when lower wins


def test_median_of_three_is_the_middle_value(submitter, reviewer):
    for i, value in enumerate((10, 1000, 20), start=1):
        _bench_run(submitter, reviewer, cpu="Same CPU", value=value,
                   run_id=f"33333333-0000-0000-0000-00000000000{i}")
    data = filters.leaderboard_groups(benchmark_id=BENCH)
    # median resists the outlier that a mean would follow
    assert data["groups"][0]["median"] == 20


def test_benchmarks_default_to_grouping_by_model():
    """Family is a filter, not a ranking.

    A family's median is whatever mix of its models people happened to submit -
    a dozen runs of the cheapest SKU and one of the flagship - so ranking
    families against each other measures submission habits rather than hardware.
    """
    assert filters.group_field_for("bench.gpu.clpeak") == "gpu"
    assert filters.group_field_for("bench.cpu.sysbench-multi") == "cpu"


def test_family_is_not_offered_as_a_grouping():
    assert "cpu_family" not in filters.GROUP_FIELDS
    assert "gpu_family" not in filters.GROUP_FIELDS
    # Still available to narrow with, which is the useful half.
    assert "cpu_family" in filters.FAMILY_GROUPS
    assert "gpu_family" in filters.FAMILY_GROUPS


def test_leaderboard_page_defaults_to_grouped_view(client, submitter, reviewer):
    _bench_run(submitter, reviewer, cpu="EPYC 9354", value=100,
               run_id="44444444-0000-0000-0000-000000000001")
    resp = client.get(reverse("benchmarks:leaderboard", args=[BENCH]))
    assert resp.status_code == 200
    assert "Median" in resp.text
    assert "bench-bar" in resp.text          # the ranked bars render
    assert "EPYC 9354" in resp.text


def test_selecting_a_cpu_drills_down_to_individual_runs(client, submitter, reviewer):
    _bench_run(submitter, reviewer, cpu="EPYC 9354", value=100,
               run_id="55555555-0000-0000-0000-000000000001")
    _bench_run(submitter, reviewer, cpu="Xeon 6430", value=150,
               run_id="55555555-0000-0000-0000-000000000002")

    resp = client.get(
        reverse("benchmarks:leaderboard", args=[BENCH]), {"cpu": "EPYC 9354"}
    )
    assert "individual run" in resp.text
    assert "Submitted by" in resp.text      # the per-run columns
    # scope to the results region: the filtered-out CPU still appears as an
    # option in the filter dropdown, which is correct
    results = resp.text.split('id="leaderboard"', 1)[1]
    assert "EPYC 9354" in results
    assert "Xeon 6430" not in results


def test_group_none_shows_every_run(client, submitter, reviewer):
    _bench_run(submitter, reviewer, cpu="EPYC 9354", value=100,
               run_id="66666666-0000-0000-0000-000000000001")
    _bench_run(submitter, reviewer, cpu="Xeon 6430", value=150,
               run_id="66666666-0000-0000-0000-000000000002")
    resp = client.get(
        reverse("benchmarks:leaderboard", args=[BENCH]), {"group": "none"}
    )
    assert "2 individual runs" in resp.text


def test_unpublished_runs_never_reach_the_leaderboard(submitter, reviewer):
    _bench_run(submitter, reviewer, cpu="Public CPU", value=100,
               run_id="77777777-0000-0000-0000-000000000001")
    # ingested but never approved
    inventory = f.default_inventory()
    inventory["summary"]["cpus"][0]["model"] = "Secret CPU"
    report = f.make_report(
        run_types=["benchmark"], inventory=inventory,
        run_id="77777777-0000-0000-0000-000000000002",
        results=[f.benchmark_result(test_id=BENCH)],
    )
    ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(f.build_bundle(report)),
        source="api",
    )
    data = filters.leaderboard_groups(benchmark_id=BENCH)
    assert [g["key"] for g in data["groups"]] == ["Public CPU"]


def test_benchmarks_index_headline_metrics_are_capped(client, submitter, reviewer):
    """The bug this replaced: every primary metric was concatenated into one
    unreadable blob."""
    metrics = [
        {"name": f"m{i}", "value": i * 10, "unit": "MB/s",
         "direction": "higher_is_better", "primary": True}
        for i in range(1, 6)
    ]
    report = f.make_report(
        run_types=["benchmark"],
        run_id="88888888-0000-0000-0000-000000000001",
        results=[f.benchmark_result(metrics=metrics)],
    )
    run = ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(f.build_bundle(report)),
        source="api",
    )
    services.approve_run(release(run), by=reviewer)

    resp = client.get(reverse("benchmarks:index"))
    assert "more ·" in resp.text  # the overflow count, not 5 inline metrics


# --- families resolved dynamically from curated patterns ----------------------


@pytest.fixture
def epyc_family():
    """The seeded EPYC 9004 family - certification granularity comes from the
    starting set shipped in a data migration, not from test setup."""
    from lumina.hardware.models import Component

    return Component.objects.get(name="AMD EPYC 9004 Series")


def test_filtering_by_family_keeps_its_models_apart(submitter, reviewer,
                                                    epyc_family):
    """Narrowing to a generation and comparing the models in it is the real
    question. Collapsing them into one family figure is not."""
    _bench_run(submitter, reviewer, cpu="AMD EPYC 9354 32-Core Processor",
               value=100, run_id="aaaa0001-0000-4000-8000-000000000001")
    _bench_run(submitter, reviewer, cpu="AMD EPYC 9454 48-Core Processor",
               value=200, run_id="aaaa0001-0000-4000-8000-000000000002")
    _bench_run(submitter, reviewer, cpu="Intel(R) Xeon(R) Gold 6430",
               value=150, run_id="aaaa0001-0000-4000-8000-000000000003")

    data = filters.leaderboard_groups(
        benchmark_id=BENCH,
        params={"cpu_family": ["AMD EPYC 9004 Series"]},
    )

    keys = {g["key"] for g in data["groups"]}
    assert keys == {"AMD EPYC 9354 32-Core Processor",
                    "AMD EPYC 9454 48-Core Processor"}
    assert all(group["runs"] == 1 for group in data["groups"])


def test_model_grouping_keeps_models_separate(submitter, reviewer, epyc_family):
    """Benchmarks care about specific models, so the model level never rolls up."""
    _bench_run(submitter, reviewer, cpu="AMD EPYC 9354 32-Core Processor",
               value=100, run_id="aaaa0002-0000-4000-8000-000000000001")
    _bench_run(submitter, reviewer, cpu="AMD EPYC 9454 48-Core Processor",
               value=200, run_id="aaaa0002-0000-4000-8000-000000000002")

    data = filters.leaderboard_groups(benchmark_id=BENCH, group_by="cpu")
    keys = {g["key"] for g in data["groups"]}
    assert keys == {"AMD EPYC 9354 32-Core Processor",
                    "AMD EPYC 9454 48-Core Processor"}


def test_adding_a_pattern_makes_a_family_filterable_without_backfill(submitter,
                                                                     reviewer):
    """Families resolve at read time, so curating a pattern in the admin
    immediately applies to results already in the database."""
    from lumina.hardware.models import Component, ComponentKind
    from lumina.vendors.models import Vendor

    # a part no seeded family covers yet
    _bench_run(submitter, reviewer, cpu="Acme Foocore 1234",
               value=100, run_id="aaaa0003-0000-4000-8000-000000000001")
    unmatched = filters.leaderboard_groups(
        benchmark_id=BENCH, params={"cpu_family": ["Acme Foocore Series"]})
    assert unmatched["groups"] == []

    acme, _ = Vendor.objects.get_or_create(name="Acme")
    Component.objects.create(
        vendor=acme, name="Acme Foocore Series", kind=ComponentKind.cpu.value,
        role="family", model_patterns=[r"Foocore 1[0-9]{3}"],
    )

    matched = filters.leaderboard_groups(
        benchmark_id=BENCH, params={"cpu_family": ["Acme Foocore Series"]})
    assert [g["key"] for g in matched["groups"]] == ["Acme Foocore 1234"]


def test_family_filter_narrows_to_its_models(client, submitter, reviewer, epyc_family):
    _bench_run(submitter, reviewer, cpu="AMD EPYC 9354 32-Core Processor",
               value=100, run_id="aaaa0004-0000-4000-8000-000000000001")
    _bench_run(submitter, reviewer, cpu="Intel(R) Xeon(R) Gold 6430",
               value=150, run_id="aaaa0004-0000-4000-8000-000000000002")

    resp = client.get(
        reverse("benchmarks:leaderboard", args=[BENCH]),
        {"cpu_family": "AMD EPYC 9004 Series"},
    )
    results = resp.text.split('id="leaderboard"', 1)[1]
    assert "EPYC 9354" in results
    assert "Xeon" not in results
    # picking a family drills to the models inside it
    assert "CPU model" in results


def test_certification_ties_to_the_family_not_the_model(
    submitter, reviewer, epyc_family
):
    """Validation cares about families: a passing run on an EPYC 9354 attests
    the 9004 Series entry rather than spawning a per-model listing."""
    from lumina.hardware.models import Component

    inventory = f.default_inventory()
    inventory["summary"]["cpus"][0]["model"] = "AMD EPYC 9354 32-Core Processor"
    inventory["summary"]["cpus"][0]["vendor"] = "AuthenticAMD"
    report = f.make_report(
        run_types=["validate"],
        results=[f.validate_result("validate.cpu.functional")],
        inventory=inventory,
    )
    run = ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(f.build_bundle(report)),
        source="api",
    )
    services.approve_run(release(run), by=reviewer)

    tied = run.listing_components.filter(kind="cpu")
    assert [c.name for c in tied] == ["AMD EPYC 9004 Series"]
    epyc_family.refresh_from_db()
    assert epyc_family.attestation_count == 1
    # no per-model CPU listing was created alongside it
    assert not Component.objects.filter(
        kind="cpu", name="EPYC 9354"
    ).exists()


# --- the index's own headings ----------------------------------------------------------
#
# Reported as GPU benchmarks not being listed on the benchmarks page. They were listed; the section
# heading came from ``capfirst`` on the suite's category slug, so somebody scanning for "GPU" was
# reading past a heading that said "Gpu".


def test_the_index_heads_each_section_with_the_acronym(submitter, reviewer):
    """Through the real page, because the label is only applied in the view: the catalog rows are
    dicts from an aggregate, so a model property could not carry it."""
    _bench_run(submitter, reviewer, cpu="EPYC 9354", value=100,
               run_id="22222222-0000-0000-0000-000000000001")
    _bench_run(submitter, reviewer, cpu="EPYC 9354", value=42,
               run_id="22222222-0000-0000-0000-000000000002",
               benchmark="bench.gpu.clpeak", unit="GFLOPS")

    body = client_get_index()

    assert 'fw-semibold">GPU<' in body
    assert 'fw-semibold">CPU<' in body
    # The defect itself, asserted on the heading rather than anywhere the words appear: the label
    # "GPU compute" contains "GPU" and would have passed a looser check with the bug still present.
    assert 'fw-semibold">Gpu<' not in body
    assert 'fw-semibold">Cpu<' not in body


def test_two_slugs_with_one_heading_open_one_section(submitter, reviewer):
    """``regroup`` groups only *adjacent* rows and the query orders by slug, so two slugs sharing a
    heading open two identical sections unless the rows are sorted by that heading.

    ``disk`` and ``storage`` both read "Storage" and sort either side of everything in between,
    which is the case that actually breaks. ``mem`` and ``memory`` happen to be adjacent, so they
    would pass either way and prove nothing."""
    _bench_run(submitter, reviewer, cpu="EPYC 9354", value=1,
               run_id="33333333-0000-0000-0000-000000000001",
               benchmark="bench.disk.fio-randread", unit="IOPS")
    _bench_run(submitter, reviewer, cpu="EPYC 9354", value=2,
               run_id="33333333-0000-0000-0000-000000000002",
               benchmark="bench.gpu.clpeak", unit="GFLOPS")
    _bench_run(submitter, reviewer, cpu="EPYC 9354", value=3,
               run_id="33333333-0000-0000-0000-000000000003",
               benchmark="bench.storage.latency", unit="ns",
               direction="lower_is_better")

    body = client_get_index()

    assert body.count('fw-semibold">Storage<') == 1


def test_majority_direction_outvotes_a_single_dissenter(submitter, reviewer):
    """The helper directly, because the two leaderboard functions both lean on it and an
    integration test through either one can be satisfied by the other's ordering.

    Three rows say lower-is-better, one says higher. The metric's direction is the majority, so a
    single mislabelled submission cannot set it.
    """
    from lumina.results.models import BenchmarkResult, MetricDirection

    bench = "bench.compile.majority"
    for i, direction in enumerate(
        ["lower_is_better", "lower_is_better", "lower_is_better", "higher_is_better"], start=1
    ):
        _bench_run(submitter, reviewer, cpu=f"CPU {i}", value=10 * i, unit="s",
                   direction=direction, benchmark=bench,
                   run_id=f"55555555-0000-0000-0000-00000000000{i}")

    rows = BenchmarkResult.objects.filter(benchmark_id=bench)
    assert filters._majority_direction(rows) == MetricDirection.LOWER


def test_one_mislabelled_row_cannot_invert_the_ranking(submitter, reviewer):
    """Direction is a fact about the metric, and a single submission must not decide it.

    The leaderboard used to read the direction off ``.first()``, the earliest-submitted row, so a
    submitter who got in first with the wrong label flipped lower-is-better to higher-is-better for
    the whole metric and promoted the worst result to the top. The dissenting row is created
    **first** here on purpose, which is exactly the position the old code trusted; direction is a
    majority now, so being first no longer decides it.
    """
    bench = "bench.compile.kernel"
    # The mislabelled row first, so ``.first()`` (the old logic) would pick it.
    _bench_run(submitter, reviewer, cpu="Slow CPU", value=999, unit="s",
               direction="higher_is_better", benchmark=bench,
               run_id="44444444-0000-0000-0000-000000000001")
    _bench_run(submitter, reviewer, cpu="Fast CPU", value=50, unit="s",
               direction="lower_is_better", benchmark=bench,
               run_id="44444444-0000-0000-0000-000000000002")
    _bench_run(submitter, reviewer, cpu="Mid CPU", value=120, unit="s",
               direction="lower_is_better", benchmark=bench,
               run_id="44444444-0000-0000-0000-000000000003")

    # The flat leaderboard, where direction sets the ordering directly. Majority is lower-is-better
    # (2 to 1), so the fastest run ranks first; the old first-row logic would have ranked the 999s
    # result on top.
    ranked = list(filters.filter_leaderboard(benchmark_id=bench))
    assert [float(r.value) for r in ranked] == [50, 120, 999]

    # And the grouped view agrees.
    data = filters.leaderboard_groups(benchmark_id=bench)
    assert data["groups"][0]["key"] == "Fast CPU"
    assert data["groups"][0]["rank"] == 1


def client_get_index() -> str:
    from django.test import Client

    return Client().get(reverse("benchmarks:index")).content.decode()
