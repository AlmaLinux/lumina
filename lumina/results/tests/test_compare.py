"""Comparing hardware models, averaged over every run of them.

Not run against run: "how does this CPU compare to that one" is the question, and
a single run answers it with a sample size of one. Each column is a model, each
cell the median of its published runs.

Three properties carry the page, and each has a way of being quietly wrong:
direction (lower-is-better rows read backwards under a signed percentage),
benchmark version (aggregating across a version bump blends two different
quantities), and socket count (averaging 1P and 2P results describes neither).
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone

from lumina.results import compare, ingest
from lumina.results.models import BenchmarkResult, TestRun
from lumina.results.tests import factories as f

pytestmark = pytest.mark.django_db

COUNTER = {"n": 0}


@pytest.fixture(autouse=True)
def releases():
    """Release rows are seeded by a management command, not a migration."""
    from lumina.releases.models import AlmaLinuxRelease

    for major in (8, 9, 10):
        AlmaLinuxRelease.objects.get_or_create(major=major,
                                               defaults={"supported": True})


def make_run(*, cpu, sockets=1, cores=8, threads=16, metrics=None,
             published=True, submitter=None, release="9.6", gpu=None):
    """A published benchmark run for one CPU, with the metrics given.

    ``gpu`` overrides the reported GPU model, so a test can build two distinct GPUs to compare;
    left unset it keeps the default inventory's card.
    """
    COUNTER["n"] += 1
    inventory = f.default_inventory()
    inventory["summary"]["cpus"] = [{
        "model": cpu, "vendor": "GenuineIntel", "sockets": sockets,
        "cores": cores, "threads": threads, "max_mhz": "3400.0",
        "flags_virt": "vmx",
    }]
    if gpu is not None:
        inventory["summary"]["gpus"][0]["smi_name"] = gpu
    report = f.make_report(
        run_types=["benchmark"], version_id=release,
        run_id=f"a0000000-0000-0000-0000-{COUNTER['n']:012d}",
        results=[f.benchmark_result("bench.cpu.sysbench-multi", category="cpu")],
        inventory=inventory,
    )
    user = submitter or User.objects.filter(username="cmp").first() \
        or User.objects.create_user("cmp", email="cmp@example.com")
    run = ingest.ingest_bundle(
        submitter=user, bundle_file=f.as_upload(f.build_bundle(report)),
        source="api",
    )
    run.benchmarks.all().delete()
    from lumina.results.component_match import normalize_gpu_model
    for spec in metrics or []:
        benchmark_id = spec.get("benchmark_id", "bench.cpu.sysbench-multi")
        # A GPU benchmark row carries the card it ran on. For a single-GPU run that is the run's
        # own gpu, so default device from it; a test wanting two cards on one run passes device
        # (and device_ordinal) explicitly. Non-GPU rows stay blank, as ingest leaves them.
        is_gpu = benchmark_id.startswith("bench.gpu.")
        device_raw = spec.get("device", (gpu or "") if is_gpu else "")
        device_model = spec.get("device_model") or (
            normalize_gpu_model(device_raw) if device_raw else ""
        )
        BenchmarkResult.objects.create(
            run=run,
            benchmark_id=benchmark_id,
            benchmark_version=spec.get("version", "1"),
            category=spec.get("category", "cpu"),
            metric=spec.get("metric", "events_per_sec"),
            value=spec["value"],
            unit=spec.get("unit", "events/s"),
            direction=spec.get("direction", "higher_is_better"),
            is_primary=spec.get("primary", True),
            device_raw=device_raw,
            device_model=device_model,
            device_pci_id=spec.get("device_pci_id", ""),
            device_ordinal=spec.get("device_ordinal", 0),
        )
    if published:
        run.status = TestRun.STATUS_APPROVED
        run.published_at = timezone.now()
        run.save(update_fields=["status", "published_at"])
    return run


def cells_of(data, benchmark_id, metric="events_per_sec"):
    for group in data["groups"]:
        for row in group["rows"]:
            if row["benchmark_id"] == benchmark_id and row["metric"] == metric:
                return row
    raise AssertionError(f"no row for {benchmark_id}/{metric}")


# --- averaging ----------------------------------------------------------------


def test_a_column_is_the_median_of_its_runs():
    """Three runs of one CPU produce one column, not three."""
    for value in (100, 200, 900):
        make_run(cpu="Xeon A", metrics=[{"value": value}])

    data = compare.compare_subjects("cpu", [compare.make_key("Xeon A", 1)])

    row = cells_of(data, "bench.cpu.sysbench-multi")
    assert row["cells"][0]["display"] == "200"
    assert row["cells"][0]["samples"] == 3


def test_the_median_resists_one_bad_run():
    """A mean would let a single throttled run move the model's figure."""
    for value in (1000, 1010, 1020, 5):
        make_run(cpu="Xeon A", metrics=[{"value": value}])

    data = compare.compare_subjects("cpu", [compare.make_key("Xeon A", 1)])

    # Median of the four is 1005; the mean would be 758.
    assert cells_of(data, "bench.cpu.sysbench-multi")["cells"][0]["display"] == "1,005"


def test_the_sample_count_and_spread_are_shown():
    """A median of one run and a median of ten deserve different confidence."""
    for value in (100, 300):
        make_run(cpu="Xeon A", metrics=[{"value": value}])

    cell = cells_of(
        compare.compare_subjects("cpu", [compare.make_key("Xeon A", 1)]),
        "bench.cpu.sysbench-multi",
    )["cells"][0]

    assert cell["samples"] == 2
    assert cell["spread"] == "100–300"


# --- socket count is identity, not an average ---------------------------------


def test_one_and_two_socket_results_are_separate_columns():
    """Averaging them would produce a figure describing neither machine."""
    make_run(cpu="Xeon A", sockets=1, cores=8, metrics=[{"value": 100}])
    make_run(cpu="Xeon A", sockets=2, cores=16, metrics=[{"value": 200}])

    options = compare.subject_options("cpu")

    assert [o["key"] for o in options] == [
        compare.make_key("Xeon A", 1), compare.make_key("Xeon A", 2),
    ]
    assert [o["label"] for o in options] == ["Xeon A", "Xeon A ×2"]


def test_a_two_socket_column_only_aggregates_two_socket_runs():
    make_run(cpu="Xeon A", sockets=1, metrics=[{"value": 100}])
    make_run(cpu="Xeon A", sockets=2, metrics=[{"value": 200}])
    make_run(cpu="Xeon A", sockets=2, metrics=[{"value": 220}])

    data = compare.compare_subjects("cpu", [
        compare.make_key("Xeon A", 2), compare.make_key("Xeon A", 1),
    ])

    row = cells_of(data, "bench.cpu.sysbench-multi")
    assert row["cells"][0]["display"] == "210"
    assert row["cells"][0]["samples"] == 2
    assert row["cells"][1]["display"] == "100"


def test_a_model_string_containing_the_key_separator_still_resolves():
    """Keys are model|sockets; a pipe in a model name would break the split."""
    model, sockets = compare.split_key("Xeon A|2")
    assert (model, sockets) == ("Xeon A", 2)
    # No socket suffix at all: the whole string is the model.
    assert compare.split_key("Xeon A") == ("Xeon A", None)


# --- overall marks (reused from the scores leaderboard) -----------------------


def _single_multi(cpu, sockets, *, single, multi):
    make_run(cpu=cpu, sockets=sockets, metrics=[
        {"benchmark_id": "bench.cpu.sysbench-single", "value": single},
        {"benchmark_id": "bench.cpu.sysbench-multi", "value": multi},
    ])


def test_the_compare_carries_the_overall_marks_and_separates_sockets():
    """The same composite Marks the leaderboard ranks on, per subject - and 1P vs 2P of one chip are
    separate columns, so you can read how it scales: same single-core, higher multi-core."""
    _single_multi("Xeon A", 1, single=100, multi=1000)
    _single_multi("Xeon A", 2, single=100, multi=2000)

    data = compare.compare_subjects("cpu", [
        compare.make_key("Xeon A", 1), compare.make_key("Xeon A", 2),
    ])

    assert data["has_scores"]
    one, two = data["subjects"]
    assert one["sockets"] == 1 and two["sockets"] == 2
    assert one["marks"]["single"] == two["marks"]["single"], "single-core does not scale with sockets"
    assert two["marks"]["multi"] > one["marks"]["multi"], "the dual-socket board scores higher multi"


def test_the_compare_marks_match_the_leaderboards(client):
    """Reuse, not a second formula: a model's mark on the compare page is the one on the board."""
    from lumina.results import benchmark_scoring

    _single_multi("Xeon A", 1, single=100, multi=1000)
    _single_multi("Xeon B", 1, single=400, multi=4000)

    board = {(e["model"], e["sockets"]): e for e in benchmark_scoring.cpu_leaderboard()}
    data = compare.compare_subjects("cpu", [
        compare.make_key("Xeon A", 1), compare.make_key("Xeon B", 1),
    ])

    for subject in data["subjects"]:
        assert subject["marks"]["overall"] == board[(subject["model"], subject["sockets"])]["overall"]


def test_the_compare_page_renders_the_overall_score_section(client):
    _single_multi("Xeon A", 1, single=100, multi=1000)
    _single_multi("Xeon B", 1, single=200, multi=2000)

    body = client.get(reverse("benchmarks:compare"), {
        "kind": "cpu",
        "subject": [compare.make_key("Xeon A", 1), compare.make_key("Xeon B", 1)],
    }).content.decode()

    assert "Overall score" in body
    assert "Single-core" in body and "Multi-core" in body


# --- deltas -------------------------------------------------------------------


def test_higher_is_better_deltas_read_forwards():
    make_run(cpu="Slow", metrics=[{"value": 100}])
    make_run(cpu="Fast", metrics=[{"value": 150}])

    data = compare.compare_subjects("cpu", [
        compare.make_key("Slow", 1), compare.make_key("Fast", 1),
    ])

    delta = cells_of(data, "bench.cpu.sysbench-multi")["cells"][1]["delta"]
    assert delta["text"] == "+50%"
    assert delta["better"] is True


def test_lower_is_better_deltas_read_backwards_on_purpose():
    """Memory latency is nanoseconds. Less of it is the whole point.

    A signed percentage alone would show -40% next to the word "worse" on every
    latency row, which is the reverse of the truth.
    """
    latency = {"benchmark_id": "bench.mem.latency", "category": "memory",
               "metric": "latency_64m", "unit": "ns",
               "direction": "lower_is_better"}
    make_run(cpu="Slow", metrics=[{**latency, "value": 100}])
    make_run(cpu="Fast", metrics=[{**latency, "value": 60}])

    data = compare.compare_subjects("cpu", [
        compare.make_key("Slow", 1), compare.make_key("Fast", 1),
    ])

    row = cells_of(data, "bench.mem.latency", "latency_64m")
    assert row["lower_is_better"] is True
    delta = row["cells"][1]["delta"]
    assert delta["text"] == "-40%"
    assert delta["better"] is True


def test_an_identical_result_is_not_a_regression():
    """A compression ratio is the same on every machine; "+0% worse" is noise."""
    ratio = {"metric": "ratio", "unit": "x", "benchmark_id": "bench.compress.xz",
             "category": "compression"}
    make_run(cpu="A", metrics=[{**ratio, "value": "6.71"}])
    make_run(cpu="B", metrics=[{**ratio, "value": "6.71"}])

    data = compare.compare_subjects("cpu", [
        compare.make_key("A", 1), compare.make_key("B", 1),
    ])

    delta = cells_of(data, "bench.compress.xz", "ratio")["cells"][1]["delta"]
    assert delta["text"] == "no change"
    assert delta["better"] is None


def test_the_first_column_is_the_baseline_and_carries_no_delta():
    make_run(cpu="A", metrics=[{"value": 100}])
    make_run(cpu="B", metrics=[{"value": 150}])

    data = compare.compare_subjects("cpu", [
        compare.make_key("A", 1), compare.make_key("B", 1),
    ])

    cells = cells_of(data, "bench.cpu.sysbench-multi")["cells"]
    assert cells[0]["is_baseline"] is True
    assert cells[0]["delta"] is None
    assert cells[1]["delta"] is not None


def test_reordering_the_selection_changes_the_baseline():
    make_run(cpu="A", metrics=[{"value": 100}])
    make_run(cpu="B", metrics=[{"value": 200}])

    forwards = compare.compare_subjects("cpu", [
        compare.make_key("A", 1), compare.make_key("B", 1)])
    backwards = compare.compare_subjects("cpu", [
        compare.make_key("B", 1), compare.make_key("A", 1)])

    assert cells_of(forwards, "bench.cpu.sysbench-multi")["cells"][1]["delta"]["text"] == "+100%"
    assert cells_of(backwards, "bench.cpu.sysbench-multi")["cells"][1]["delta"]["text"] == "-50%"


# --- version comparability ----------------------------------------------------


def test_columns_are_pinned_to_one_benchmark_version():
    """Version 1 of the stress-ng benchmarks published sys time as a throughput.

    Comparing that against version 2 would invent a difference of several orders
    of magnitude out of a parser fix, so only the newest version present is
    shown and the older column reads as not measured.
    """
    matrix = {"benchmark_id": "bench.cpu.stressng-matrix",
              "metric": "bogo_ops_per_sec", "unit": "bogo-ops/s"}
    make_run(cpu="Old", metrics=[{**matrix, "version": "1", "value": "0.13"}])
    make_run(cpu="New", metrics=[{**matrix, "version": "2", "value": 14710}])

    data = compare.compare_subjects("cpu", [
        compare.make_key("Old", 1), compare.make_key("New", 1),
    ])

    row = cells_of(data, "bench.cpu.stressng-matrix", "bogo_ops_per_sec")
    assert row["version"] == "2"
    assert row["mixed_versions"] is True
    assert row["cells"][0]["present"] is False      # Old has nothing at v2
    assert row["cells"][1]["display"] == "14,710"


def test_runs_of_one_model_on_two_versions_use_only_the_newer():
    matrix = {"benchmark_id": "bench.cpu.stressng-matrix",
              "metric": "bogo_ops_per_sec", "unit": "bogo-ops/s"}
    make_run(cpu="A", metrics=[{**matrix, "version": "1", "value": "0.13"}])
    make_run(cpu="A", metrics=[{**matrix, "version": "2", "value": 14710}])
    make_run(cpu="A", metrics=[{**matrix, "version": "2", "value": 14000}])

    data = compare.compare_subjects("cpu", [compare.make_key("A", 1)])

    cell = cells_of(data, "bench.cpu.stressng-matrix", "bogo_ops_per_sec")["cells"][0]
    assert cell["samples"] == 2
    assert cell["display"] == "14,355"


# --- alignment and specs ------------------------------------------------------


def test_a_metric_only_one_column_has_still_gets_a_row():
    """Blank is informative: it says that model has no result for this test.

    Shown with a platform benchmark only one column has, not a GPU benchmark in a CPU comparison:
    that would be the cross-kind spill the picker fix removes, so a CPU table no longer carries GPU
    rows at all.
    """
    make_run(cpu="A", metrics=[{"value": 100}])
    make_run(cpu="B", metrics=[
        {"value": 120},
        {"benchmark_id": "bench.mem.latency", "category": "memory", "metric": "latency_64m",
         "unit": "ns", "value": 90, "direction": "lower_is_better"},
    ])

    data = compare.compare_subjects("cpu", [
        compare.make_key("A", 1), compare.make_key("B", 1),
    ])

    row = cells_of(data, "bench.mem.latency", "latency_64m")
    assert row["cells"][0]["present"] is False
    assert row["cells"][1]["display"] == "90"


def test_several_metrics_of_one_benchmark_are_distinguishable():
    """Four rows titled "Memory bandwidth" with different numbers is not a table."""
    band = {"benchmark_id": "bench.mem.bandwidth", "category": "memory",
            "unit": "MB/s"}
    make_run(cpu="A", metrics=[
        {**band, "metric": "triad_bandwidth", "value": 100},
        {**band, "metric": "copy_bandwidth", "value": 110},
    ])
    make_run(cpu="B", metrics=[
        {**band, "metric": "triad_bandwidth", "value": 200},
        {**band, "metric": "copy_bandwidth", "value": 210},
    ])

    data = compare.compare_subjects("cpu", [
        compare.make_key("A", 1), compare.make_key("B", 1),
    ])

    rows = [r for g in data["groups"] for r in g["rows"]
            if r["benchmark_id"] == "bench.mem.bandwidth"]
    assert len(rows) == 2
    assert all(row["show_metric"] for row in rows)
    assert {row["metric_label"] for row in rows} == {
        "triad bandwidth", "copy bandwidth",
    }


def test_a_gpu_row_names_the_api_that_produced_it():
    """The comparison rows go through the same ``metric_label`` the run page and the leaderboard use,
    so this comes free with the GPU breakdown - and it matters most here, where two columns of
    numbers sit side by side: "vulkan global memory bandwidth" reads as prose and buries the one
    word that says which software stack produced the figure."""
    clpeak = {"benchmark_id": "bench.gpu.clpeak", "category": "gpu", "unit": "GFLOPS"}
    # A GPU comparison of two GPUs, not a CPU comparison that happens to spill GPU rows: the
    # latter is the cross-kind leak now closed, so the columns are keyed on the GPU model.
    for gpu, value in (("NVIDIA A", 100), ("NVIDIA B", 200)):
        make_run(cpu="host", gpu=gpu, metrics=[
            {**clpeak, "metric": "vulkan_single_precision_compute", "value": value},
            {**clpeak, "metric": "opencl_single_precision_compute", "value": value - 10},
        ])

    data = compare.compare_subjects("gpu", [
        compare.make_key("NVIDIA A"), compare.make_key("NVIDIA B"),
    ])

    rows = [r for g in data["groups"] for r in g["rows"]
            if r["benchmark_id"] == "bench.gpu.clpeak"]
    assert {row["metric_label"] for row in rows} == {
        "Vulkan single precision", "OpenCL single precision",
    }


def test_two_gpus_in_one_run_compare_as_separate_subjects():
    """A machine with two different GPUs contributes a benchmark row per card. Each is offered as
    its own compare subject from that single run, and comparing them shows each card's own number
    rather than the pair pooled - the point of keying GPU subjects on the row, not the run."""
    from lumina.results.component_match import normalize_gpu_model

    clpeak = {"benchmark_id": "bench.gpu.clpeak", "category": "gpu", "unit": "GFLOPS",
              "metric": "vulkan_single_precision_compute"}
    make_run(cpu="host", gpu="NVIDIA L40S", metrics=[
        {**clpeak, "value": 89000, "device": "NVIDIA L40S"},
        {**clpeak, "value": 1100, "device": "Intel(R) UHD Graphics 630"},
    ])
    nv = normalize_gpu_model("NVIDIA L40S")
    intel = normalize_gpu_model("Intel(R) UHD Graphics 630")
    assert nv != intel

    # Both cards are offered as subjects, each backed by the one run.
    options = {o["model"]: o for o in compare.subject_options("gpu")}
    assert set(options) == {nv, intel}
    assert options[nv]["runs"] == 1 and options[intel]["runs"] == 1

    # Comparing them separates the columns: each shows only its own card's figure, one sample each.
    data = compare.compare_subjects("gpu", [compare.make_key(nv), compare.make_key(intel)])
    row = cells_of(data, "bench.gpu.clpeak", "vulkan_single_precision_compute")
    assert row["cells"][0]["display"] == "89,000"
    assert row["cells"][1]["display"] == "1,100"
    assert row["cells"][0]["samples"] == 1
    assert row["cells"][1]["samples"] == 1


def test_a_cards_backends_group_as_one_gpu_subject():
    """The reported bug: one Intel iGPU named 'Intel Graphics (ARL)' by Vulkan and 'Intel Graphics'
    by OpenCL was offered as two GPUs. Tied to the card's PCI id, both are one subject now - keyed
    by the id, labeled with the more specific name - and comparing it gathers both backends."""
    clpeak = {"benchmark_id": "bench.gpu.clpeak", "category": "gpu", "unit": "GFLOPS",
              "device_pci_id": "8086:7d55"}
    make_run(cpu="host", metrics=[
        {**clpeak, "metric": "vulkan_single_precision_compute", "value": 1000,
         "device": "Intel Graphics (ARL)"},
        {**clpeak, "metric": "opencl_single_precision_compute", "value": 900,
         "device": "Intel Graphics"},
    ])

    options = compare.subject_options("gpu")
    assert len(options) == 1, options
    assert options[0]["key"] == "8086:7d55"
    assert options[0]["label"] == "Intel Graphics (ARL)"  # representative: the most specific name
    assert options[0]["runs"] == 1

    data = compare.compare_subjects("gpu", [compare.make_key("8086:7d55")])
    subject = data["subjects"][0]
    assert subject["label"] == "Intel Graphics (ARL)"
    # Both backends' metrics land under the one subject rather than being split across two.
    assert set(subject["samples"]) == {
        ("bench.gpu.clpeak", "vulkan_single_precision_compute"),
        ("bench.gpu.clpeak", "opencl_single_precision_compute"),
    }


def test_the_grouped_label_prefers_the_marketing_name_over_a_codename():
    """One AMD card: RADV/Vulkan reports 'AMD Radeon RX 7900 XTX', ROCm reports the bare 'gfx1100'.
    The subject must read as the marketing name, the same way the leaderboard names it - a plain SQL
    Max would pick 'gfx1100' (lowercase sorts after 'A') and show the codename the feature exists to
    avoid."""
    clpeak = {"benchmark_id": "bench.gpu.clpeak", "category": "gpu", "unit": "GFLOPS",
              "device_pci_id": "1002:744c"}
    make_run(cpu="host", metrics=[
        {**clpeak, "metric": "vulkan_single_precision_compute", "value": 5000,
         "device": "AMD Radeon RX 7900 XTX"},
        {**clpeak, "metric": "rocm_single_precision_compute", "value": 5200, "device": "gfx1100"},
    ])

    options = compare.subject_options("gpu")
    assert len(options) == 1
    assert options[0]["label"] == "AMD Radeon RX 7900 XTX"

    label = compare.compare_subjects("gpu", [compare.make_key("1002:744c")])["subjects"][0]["label"]
    assert label == "AMD Radeon RX 7900 XTX"


def test_backend_names_still_split_when_no_pci_id_ties_them():
    """The fallback: rows with no resolved PCI id group by device_model exactly as before, so
    nothing about the pre-PCI behaviour changes for a card that could not be tied."""
    clpeak = {"benchmark_id": "bench.gpu.clpeak", "category": "gpu", "unit": "GFLOPS",
              "metric": "vulkan_single_precision_compute"}
    make_run(cpu="host", metrics=[
        {**clpeak, "value": 1000, "device": "Intel Graphics (ARL)"},   # no device_pci_id
        {**clpeak, "value": 900, "device": "Intel Graphics"},
    ])

    models = {o["model"] for o in compare.subject_options("gpu")}
    assert models == {"Intel Graphics (ARL)", "Intel Graphics"}


def test_a_lone_metric_is_not_cluttered_with_its_own_name():
    make_run(cpu="A", metrics=[{"value": 100}])
    make_run(cpu="B", metrics=[{"value": 200}])

    row = cells_of(
        compare.compare_subjects("cpu", [
            compare.make_key("A", 1), compare.make_key("B", 1)]),
        "bench.cpu.sysbench-multi",
    )

    assert row["show_metric"] is False


def test_the_specs_flag_what_differs():
    """The differing rows are the explanation for the numbers underneath."""
    make_run(cpu="A", sockets=1, cores=4, threads=8, metrics=[{"value": 100}])
    make_run(cpu="B", sockets=1, cores=64, threads=128, metrics=[{"value": 900}])

    data = compare.compare_subjects("cpu", [
        compare.make_key("A", 1), compare.make_key("B", 1),
    ])
    specs = {row["label"]: row for row in data["specs"]}

    assert specs["Cores"]["differs"] is True
    assert specs["Cores"]["values"] == ["4", "64"]
    assert specs["Sockets"]["differs"] is False


def test_the_machine_list_is_capped():
    """A popular CPU turns up in dozens of machines."""
    for index in range(5):
        run = make_run(cpu="A", metrics=[{"value": 100}])
        run.system_product = f"Machine {index}"
        run.save(update_fields=["system_product"])

    data = compare.compare_subjects("cpu", [compare.make_key("A", 1)])
    machines = next(r for r in data["specs"] if r["label"] == "Machines tested")

    assert "more" in machines["values"][0]


def test_releases_are_listed_not_ranged():
    """Sorting release names as strings gave "AlmaLinux 10-AlmaLinux 9"."""
    for release in ("8.10", "9.6", "10.2"):
        make_run(cpu="A", metrics=[{"value": 100}], release=release)

    data = compare.compare_subjects("cpu", [compare.make_key("A", 1)])
    releases = next(r for r in data["specs"] if r["label"] == "AlmaLinux")

    assert "–" not in releases["values"][0]
    assert releases["values"][0].count(",") == 2


# --- selection and visibility -------------------------------------------------


def test_at_most_four_columns_are_accepted():
    assert compare.parse_selection(["a", "b", "c", "d", "e"]) == ["a", "b", "c", "d"]


def test_a_repeated_selection_is_not_a_duplicate_column():
    assert compare.parse_selection(["a", "b", "a"]) == ["a", "b"]


def test_comma_separated_and_repeated_params_both_work():
    assert compare.parse_selection("a,b") == ["a", "b"]
    assert compare.parse_selection(["a,b", "c"]) == ["a", "b", "c"]


def test_an_unpublished_run_is_not_averaged_in_for_the_public():
    """Embargo: the comparison must not leak an unpublished result."""
    make_run(cpu="A", metrics=[{"value": 100}])
    make_run(cpu="A", metrics=[{"value": 9000}], published=False)

    data = compare.compare_subjects(
        "cpu", [compare.make_key("A", 1)], runs=TestRun.objects.public(),
    )

    cell = cells_of(data, "bench.cpu.sysbench-multi")["cells"][0]
    assert cell["samples"] == 1
    assert cell["display"] == "100"


def test_an_unknown_kind_falls_back_rather_than_erroring():
    assert compare.compare_subjects("nonsense", [])["kind"] == compare.DEFAULT_KIND


def test_gpu_is_offered_as_a_subject_kind():
    """The same page should compare GPU models when there are GPU results."""
    assert "gpu" in compare.SUBJECT_KINDS
    assert compare.SUBJECT_KINDS["gpu"]["split_by_sockets"] is False


# --- the page -----------------------------------------------------------------


def test_the_page_renders_a_comparison(client):
    make_run(cpu="Xeon A", sockets=2, cores=44, metrics=[{"value": 62869}])
    make_run(cpu="Core B", sockets=1, cores=4, metrics=[{"value": 7640}])

    response = client.get(reverse("benchmarks:compare"), {
        "kind": "cpu",
        "subject": [compare.make_key("Core B", 1), compare.make_key("Xeon A", 2)],
    })
    body = response.content.decode()

    assert response.status_code == 200
    assert "Xeon A ×2" in body
    assert "baseline" in body
    assert "+723%" in body
    assert "better" in body


def test_the_page_asks_for_a_selection_before_comparing(client):
    make_run(cpu="A", metrics=[{"value": 100}])

    body = client.get(reverse("benchmarks:compare")).content.decode()

    assert "Pick at least 2" in body


def test_the_selection_is_a_shareable_link(client):
    make_run(cpu="A", metrics=[{"value": 100}])
    make_run(cpu="B", metrics=[{"value": 200}])
    url = (reverse("benchmarks:compare")
           + f"?subject={compare.make_key('A', 1)}&subject={compare.make_key('B', 1)}")

    first = client.get(url).content.decode()
    second = client.get(url).content.decode()

    assert "+100%" in first
    assert first == second


# --- the picker and the direction indicator -----------------------------------


def test_the_picker_is_a_search_field_not_a_multiselect(client):
    """A native multi-select cannot be scrolled through thousands of models.

    The <select> stays in the markup so the page works without JavaScript; the
    data attributes are what turn it into a search-and-add control.
    """
    make_run(cpu="A", metrics=[{"value": 100}])

    body = client.get(reverse("benchmarks:compare")).content.decode()

    assert 'data-picker="true"' in body
    assert "data-picker-ordered" in body
    assert f'data-picker-max="{compare.MAX_COMPARE}"' in body
    assert "data-picker-placeholder" in body
    assert "<select" in body          # still degrades without scripting


def test_every_metric_row_says_which_way_is_better(client):
    """"36.7 versus 113" is unreadable without knowing which direction wins."""
    latency = {"benchmark_id": "bench.mem.latency", "category": "memory",
               "metric": "latency_64m", "unit": "ns",
               "direction": "lower_is_better"}
    make_run(cpu="A", metrics=[{"value": 100}, {**latency, "value": 60}])
    make_run(cpu="B", metrics=[{"value": 200}, {**latency, "value": 90}])

    body = client.get(reverse("benchmarks:compare"), {
        "subject": [compare.make_key("A", 1), compare.make_key("B", 1)],
    }).content.decode()

    assert 'title="Lower is better"' in body
    assert 'title="Higher is better"' in body
    # Not carried by the glyph alone.
    assert "lower is better" in body
    assert "higher is better" in body


def test_the_direction_flags_are_set_per_row():
    latency = {"benchmark_id": "bench.mem.latency", "category": "memory",
               "metric": "latency_64m", "unit": "ns",
               "direction": "lower_is_better"}
    make_run(cpu="A", metrics=[{"value": 100}, {**latency, "value": 60}])

    data = compare.compare_subjects("cpu", [compare.make_key("A", 1)])

    throughput = cells_of(data, "bench.cpu.sysbench-multi")
    assert throughput["higher_is_better"] is True
    assert throughput["lower_is_better"] is False
    lat = cells_of(data, "bench.mem.latency", "latency_64m")
    assert lat["lower_is_better"] is True
    assert lat["higher_is_better"] is False


def test_an_informational_metric_claims_no_direction():
    """No arrow and no better/worse judgement where neither applies."""
    info = {"benchmark_id": "bench.mem.bandwidth", "category": "memory",
            "metric": "threads", "unit": "count", "direction": "info"}
    make_run(cpu="A", metrics=[{**info, "value": 8}])
    make_run(cpu="B", metrics=[{**info, "value": 16}])

    data = compare.compare_subjects("cpu", [
        compare.make_key("A", 1), compare.make_key("B", 1)])
    row = cells_of(data, "bench.mem.bandwidth", "threads")

    assert row["higher_is_better"] is False
    assert row["lower_is_better"] is False
    assert row["cells"][1]["delta"]["better"] is None


# --- retired metrics ----------------------------------------------------------
#
# ``test_retiring_the_ratio_clears_it_and_keeps_the_speeds`` lived here. It drove
# ``0010_drop_compression_ratio_metrics`` directly, a one-off cleanup of rows already
# collected, and the migration went away when the history was collapsed into a single
# initial - a fresh database has no such rows to clean.
#
# Nothing was lost with it, because the cleanup was never the rule. A compression ratio
# is a property of the corpus rather than the machine: every system returns the same
# figure, so a leaderboard of it is a table of ties. The suite therefore stopped
# publishing it as a metric and files it under ``details`` instead, which is enforced
# where the data is produced - ``tests/test_parsers.py::
# test_zstd_publishes_speeds_and_not_the_ratio`` in the alma-cert repository.


# --- switching what is being compared -------------------------------------------------
#
# Reported: "On the 'compare hardware' page when selecting the GPUs category, only CPUs are offered."
# The picker was right server-side all along. The kind selector sat inside the HTMX form whose target
# is the results table, so choosing GPUs swapped the table and left the model picker holding the CPU
# options the page had been rendered with. Nothing server-side could have caught it, so the test is
# about the markup, which is where the bug was.


def test_the_gpu_picker_offers_a_gpu_that_has_a_gpu_benchmark(client):
    """A GPU is offered for comparison when it has a GPU benchmark behind it."""
    make_run(cpu="EPYC 9354", gpu="NVIDIA L40S", metrics=[
        {"benchmark_id": "bench.gpu.clpeak", "category": "gpu",
         "metric": "single_precision_compute", "value": 100, "unit": "GFLOPS"}])

    page = client.get(reverse("benchmarks:compare"), {"kind": "gpu"})
    options = page.context["options"]

    assert [option["model"] for option in options] == ["NVIDIA L40S"]
    assert page.context["kind"] == "gpu"


def test_the_gpu_picker_omits_a_gpu_with_no_gpu_benchmark(client):
    """The reported bug, GPU side: a validated card whose machine ran only a CPU benchmark has
    nothing to compare as a GPU, so it must not be offered."""
    make_run(cpu="EPYC 9354", gpu="NVIDIA L40S", metrics=[
        {"benchmark_id": "bench.cpu.sysbench-multi", "category": "cpu", "value": 100}])

    page = client.get(reverse("benchmarks:compare"), {"kind": "gpu"})

    assert list(page.context["options"]) == []


def test_a_cpu_comparison_excludes_gpu_benchmark_rows():
    """A machine that ran the full suite and a GPU benchmark: comparing its CPU shows platform
    rows only. The GPU benchmark belongs to the GPU comparison, not spilled into this one."""
    for cpu, value in (("A", 100), ("B", 120)):
        make_run(cpu=cpu, gpu="NVIDIA L40S", metrics=[
            {"benchmark_id": "bench.cpu.sysbench-multi", "category": "cpu", "value": value},
            {"benchmark_id": "bench.gpu.clpeak", "category": "gpu",
             "metric": "gflops", "unit": "GFLOPS", "value": 900}])

    data = compare.compare_subjects("cpu", [compare.make_key("A", 1), compare.make_key("B", 1)])
    ids = {r["benchmark_id"] for g in data["groups"] for r in g["rows"]}

    assert "bench.cpu.sysbench-multi" in ids
    assert "bench.gpu.clpeak" not in ids


def test_a_gpu_comparison_excludes_cpu_benchmark_rows():
    """The mirror: a GPU comparison shows GPU benchmarks, not the CPU benchmarks its host ran."""
    for gpu, value in (("NVIDIA A", 100), ("NVIDIA B", 200)):
        make_run(cpu="host", gpu=gpu, metrics=[
            {"benchmark_id": "bench.cpu.sysbench-multi", "category": "cpu", "value": 500},
            {"benchmark_id": "bench.gpu.clpeak", "category": "gpu",
             "metric": "gflops", "unit": "GFLOPS", "value": value}])

    data = compare.compare_subjects(
        "gpu", [compare.make_key("NVIDIA A"), compare.make_key("NVIDIA B")])
    ids = {r["benchmark_id"] for g in data["groups"] for r in g["rows"]}

    assert "bench.gpu.clpeak" in ids
    assert "bench.cpu.sysbench-multi" not in ids


def test_the_cpu_picker_omits_a_cpu_with_only_a_gpu_benchmark():
    """The reported bug, CPU side: a GPU benchmark run on a cloud box records the host CPU, which
    has no CPU benchmark of its own. It must not be offered as a CPU to compare, though its GPU is
    still offered on the GPU side, where there is something to show."""
    make_run(cpu="Intel(R) Xeon(R) CPU @ 2.20GHz", gpu="NVIDIA L40S", metrics=[
        {"benchmark_id": "bench.gpu.clpeak", "category": "gpu",
         "metric": "single_precision_compute", "value": 100, "unit": "GFLOPS"}])

    assert compare.subject_options("cpu") == []
    assert [o["model"] for o in compare.subject_options("gpu")] == ["NVIDIA L40S"]


def _form_containing(body: str, needle: str) -> str:
    """The ``<form>`` element that encloses ``needle``.

    Written out because the whole defect was which form a control sat in, and asserting that needs
    the enclosing element rather than the presence of a string somewhere on the page.
    """
    at = body.index(needle)
    start = body.rindex("<form", 0, at)
    end = body.index("</form>", at)
    return body[start:end]


def test_the_kind_selector_is_not_inside_the_table_only_form(client):
    """The defect itself. A control that changes which hardware is being compared cannot live in a
    form that only replaces the results table, because the picker beside it must change too."""
    make_run(cpu="EPYC 9354", metrics=[{"value": 100}])

    body = client.get(reverse("benchmarks:compare")).content.decode()

    kind_form = _form_containing(body, 'id="compare-kind"')
    assert 'hx-target="#compare-table"' not in kind_form
    # It navigates instead, and does so on change rather than needing a button found and pressed.
    assert "data-submit-on-change" in kind_form
    # And it carries no subject: a key selected under one kind means nothing under the other, so
    # switching has to drop them rather than carry them across.
    assert 'name="subject"' not in kind_form


def test_the_subject_picker_stays_on_htmx_and_keeps_its_kind(client):
    """Changing which models are compared changes only the table, and re-rendering the picker
    mid-selection would throw away the search widget's state. It has to carry the kind, though, or
    an HTMX subject change would silently fall back to comparing CPUs."""
    make_run(cpu="EPYC 9354", metrics=[{"value": 100}])

    body = client.get(reverse("benchmarks:compare"), {"kind": "gpu"}).content.decode()

    subject_form = _form_containing(body, 'id="compare-subjects"')
    assert 'hx-target="#compare-table"' in subject_form
    assert '<input type="hidden" name="kind" value="gpu">' in subject_form
