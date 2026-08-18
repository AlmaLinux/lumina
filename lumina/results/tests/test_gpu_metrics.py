"""Reading a clpeak metric name, and the pages that break GPU results down by it.

Asked for: GPU benchmarks should break down by clpeak's categories and by API type. What existed was
a flat ``<select>`` of eleven raw identifiers on the leaderboard and a column of the same on every
run page, so finding the OpenCL bandwidth figure required knowing the suite's naming scheme, and
nothing anywhere said which API a number came from.

The API is the load-bearing part. A CUDA figure and an OpenCL figure for one card measure different
software stacks, and whichever is faster says as much about the driver as about the silicon, so these
tests hold the API in front of every name rather than treating it as decoration.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import Group, User
from django.urls import reverse

from lumina.results import gpu_metrics, ingest, services
from lumina.results.highlights import metric_label
from lumina.results.tests import factories as f
from lumina.results.tests.helpers import release

pytestmark = pytest.mark.django_db


# --- reading the name -----------------------------------------------------------------


@pytest.mark.parametrize("metric,api,tag", [
    ("vulkan_single_precision_compute", "vulkan", "single_precision_compute"),
    ("opencl_global_memory_bandwidth", "opencl", "global_memory_bandwidth"),
    ("cuda_kernel_launch_latency", "cuda", "kernel_launch_latency"),
    ("rocm_integer_compute", "rocm", "integer_compute"),
    ("oneapi_transfer_bandwidth", "oneapi", "transfer_bandwidth"),
])
def test_the_api_is_read_off_the_front_of_the_name(metric, api, tag):
    assert gpu_metrics.split(metric) == (api, tag)


@pytest.mark.parametrize("metric", [
    "events_per_sec", "copy_bandwidth", "triad_bandwidth", "ops_per_sec", "", "vulkan_",
])
def test_a_metric_that_names_no_api_is_left_alone(metric):
    """Matched against the known backends rather than split on the first underscore, so
    ``events_per_sec`` is not read as an "events" backend measuring ``per_sec``. Every non-GPU
    metric in the database is this case, and ``vulkan_`` with nothing after it names no test."""
    assert gpu_metrics.split(metric) == ("", metric.strip())
    assert gpu_metrics.is_gpu_metric(metric) is False
    assert gpu_metrics.label(metric) == ""


@pytest.mark.parametrize("tag,group", [
    ("single_precision_compute", "compute"),
    ("bfloat16_compute", "compute"),
    # The one the first rule got wrong. Its category word is in the middle of the name, so a
    # suffix test filed it under "Other" beside the Compute section it belongs in.
    ("integer_compute_int8_dp", "compute"),
    ("global_memory_bandwidth", "bandwidth"),
    ("transfer_bandwidth", "bandwidth"),
    ("kernel_launch_latency", "latency"),
    ("something_unrecognized", "other"),
])
def test_the_category_comes_from_the_words_in_the_name(tag, group):
    assert gpu_metrics.group_for(tag) == group


def test_every_test_the_suite_records_has_a_name_and_a_category():
    """The twelve portable tags in ``almacert/benchmarks/gpu.py``. A tag reaching a page with no
    label is a raw identifier in front of a reader, which is the defect being fixed."""
    for tag in gpu_metrics.TAG_ORDER:
        assert tag in gpu_metrics.TAG_LABELS, tag
        assert gpu_metrics.group_for(tag) != "other", tag


def test_an_unknown_test_still_reads_as_something():
    """A tag the suite adds before this map does. Derived rather than raised over, so the page keeps
    working until somebody gives it a better name."""
    described = gpu_metrics.describe("cuda_fp8_compute")

    assert described["api_label"] == "CUDA"
    assert described["group_label"] == "Compute"
    assert described["tag_label"] == "Fp8 compute"
    assert described["label"] == "CUDA fp8 compute"


def test_the_shared_label_helper_routes_gpu_metrics_through_this():
    """``metric_label`` feeds the comparison rows and the run table, so one change covers them.
    Underscore-stripping alone gave "vulkan global memory bandwidth", which reads as prose and
    buries the part that decides what the number means."""
    assert metric_label("vulkan_global_memory_bandwidth") == "Vulkan global memory"
    # Unchanged for everything that is not a GPU metric.
    assert metric_label("triad_bandwidth") == "triad bandwidth"


# --- breaking them down ---------------------------------------------------------------


def test_sections_are_one_per_api_and_category():
    sections = gpu_metrics.grouped([
        "vulkan_kernel_launch_latency",
        "opencl_single_precision_compute",
        "vulkan_global_memory_bandwidth",
        "vulkan_single_precision_compute",
        "opencl_global_memory_bandwidth",
    ])

    assert [section["heading"] for section in sections] == [
        "OpenCL · Compute", "OpenCL · Bandwidth",
        "Vulkan · Compute", "Vulkan · Bandwidth", "Vulkan · Latency",
    ]
    assert [entry["tag_label"] for entry in sections[2]["metrics"]] == ["Single precision"]


def test_precisions_read_in_their_own_order_not_alphabetically():
    """Alphabetical put double precision above half and single, which is not how anybody reads a
    list of precisions."""
    sections = gpu_metrics.grouped([
        "vulkan_double_precision_compute",
        "vulkan_half_precision_compute",
        "vulkan_single_precision_compute",
    ])

    assert [entry["tag_label"] for entry in sections[0]["metrics"]] == [
        "Single precision", "Double precision", "Half precision",
    ]


def test_nothing_to_group_returns_nothing():
    """How a caller chooses between sections and a flat list without asking twice."""
    assert gpu_metrics.grouped(["events_per_sec", "copy_bandwidth"]) == []
    assert gpu_metrics.grouped([]) == []


# --- the pages ------------------------------------------------------------------------


@pytest.fixture
def submitter():
    return User.objects.create_user("gpu-bencher")


@pytest.fixture
def reviewer():
    user = User.objects.create_user("gpu-rev")
    group, _ = Group.objects.get_or_create(name="reviewer")
    user.groups.add(group)
    return user


def _gpu_run(submitter, reviewer, *, run_id, metrics):
    report = f.make_report(
        run_types=["benchmark"],
        run_id=run_id,
        results=[f.benchmark_result(
            test_id="bench.gpu.clpeak",
            category="gpu",
            metrics=[
                {"name": name, "value": value, "unit": unit,
                 "direction": "higher_is_better", "primary": index == 0}
                for index, (name, value, unit) in enumerate(metrics)
            ],
        )],
    )
    run = ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(f.build_bundle(report)), source="api",
    )
    services.approve_run(release(run), by=reviewer)
    return run


CLPEAK = [
    ("vulkan_single_precision_compute", 383.0, "GFLOPS"),
    ("vulkan_global_memory_bandwidth", 17.7, "GB/s"),
    ("opencl_single_precision_compute", 350.0, "GFLOPS"),
]


def test_the_leaderboard_picker_is_grouped_by_api_and_category(client, submitter, reviewer):
    _gpu_run(submitter, reviewer, run_id="44444444-0000-0000-0000-000000000001",
             metrics=CLPEAK)

    body = client.get(reverse("benchmarks:leaderboard",
                              args=["bench.gpu.clpeak"])).content.decode()

    assert '<optgroup label="OpenCL · Compute">' in body
    assert '<optgroup label="Vulkan · Compute">' in body
    assert '<optgroup label="Vulkan · Bandwidth">' in body
    # The option reads as the test, with the API carried by the section it sits in.
    assert ">Global memory</option>" in body


def test_a_non_gpu_leaderboard_keeps_its_flat_list(client, submitter, reviewer):
    """Right where a benchmark reports two or three metrics: sections would be ceremony."""
    report = f.make_report(
        run_types=["benchmark"],
        run_id="44444444-0000-0000-0000-000000000002",
        results=[f.benchmark_result(
            test_id="bench.mem.bandwidth",
            category="memory",
            metrics=[{"name": "triad_bandwidth", "value": 10.0, "unit": "MB/s",
                      "direction": "higher_is_better", "primary": True}],
        )],
    )
    run = ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(f.build_bundle(report)), source="api",
    )
    services.approve_run(release(run), by=reviewer)

    body = client.get(reverse("benchmarks:leaderboard",
                              args=["bench.mem.bandwidth"])).content.decode()

    assert "<optgroup" not in body
    assert ">triad_bandwidth</option>" in body


def test_the_leaderboard_heading_says_which_api_produced_the_number(client, submitter, reviewer):
    _gpu_run(submitter, reviewer, run_id="44444444-0000-0000-0000-000000000003",
             metrics=CLPEAK)

    url = reverse("benchmarks:leaderboard", args=["bench.gpu.clpeak"])
    body = client.get(url, {"metric": "opencl_single_precision_compute"}).content.decode()

    assert "OpenCL" in body
    # The raw key stays on the page: it is what this URL pins and what somebody reproducing the
    # comparison needs.
    assert "opencl_single_precision_compute" in body


def test_a_runs_gpu_rows_read_by_api_then_category(client, submitter, reviewer):
    """``BenchmarkResult.Meta`` orders alphabetically by metric, which interleaved the bandwidth
    figures with the compute ones and put double precision above single."""
    run = _gpu_run(submitter, reviewer, run_id="44444444-0000-0000-0000-000000000004", metrics=[
        ("vulkan_global_memory_bandwidth", 17.7, "GB/s"),
        ("vulkan_double_precision_compute", 96.3, "GFLOPS"),
        ("vulkan_single_precision_compute", 383.0, "GFLOPS"),
        ("vulkan_kernel_launch_latency", 60.7, "us"),
    ])

    ordered = [row.metric for row in gpu_metrics.reading_order(run.benchmarks.all())]

    assert ordered == [
        "vulkan_single_precision_compute",
        "vulkan_double_precision_compute",
        "vulkan_global_memory_bandwidth",
        "vulkan_kernel_launch_latency",
    ]
    body = client.get(run.get_absolute_url()).content.decode()
    assert body.index("Single precision") < body.index("Global memory")


def test_the_run_table_names_the_api_without_repeating_it(client, submitter, reviewer):
    """The badge carries the API, so the name beside it must not carry it too: the first attempt
    rendered "Vulkan Compute - Vulkan double precision"."""
    run = _gpu_run(submitter, reviewer, run_id="44444444-0000-0000-0000-000000000005",
                   metrics=[("vulkan_double_precision_compute", 96.3, "GFLOPS")])

    body = client.get(run.get_absolute_url()).content.decode()

    assert "Vulkan Compute · Vulkan double precision" not in body
    row = run.benchmarks.first()
    assert row.gpu_api_label == "Vulkan"
    assert row.gpu_group_label == "Compute"
    assert row.gpu_tag_label == "Double precision"
    # And the raw key is still there for anyone reproducing it.
    assert "vulkan_double_precision_compute" in body
