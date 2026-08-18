"""Composite benchmark scores - the relative "Mark" an overall leaderboard ranks on.

Raw benchmark numbers do not combine: events/s, GFLOPS, and GB/s are different quantities. Each is
normalized against a reference into a dimensionless ratio, and the **geometric** mean of those
ratios - scaled so a machine at the reference scores ~1000 - is the Mark. Geometric, not arithmetic,
so a metric measured in the millions cannot swamp one measured in single digits, and a doubling
counts the same wherever it happens.

The reference for each metric is the geometric mean of the **current** public results for it,
computed live - so a Mark is relative to the field as it stands and moves as the field grows.

**Which benchmark feeds which axis is defined in ``benchmark_registry``, not here.** Each active
benchmark declares a scoring axis (``cpu-single``, ``cpu-multi``, a CPU area, or ``gpu``); this
module groups the scored metrics by that axis. So a benchmark the registry does not list - a retired
one whose old rows linger, an unknown id - is never scored, and adding one to a Mark is a registry
edit, not a change here.

Axes:
  - CPU **single-core** / **multi-core**: the ``cpu-single`` / ``cpu-multi`` benchmarks.
  - CPU **overall**: the geomean of every ``cpu-*`` area score.
  - GPU **overall**: the geomean of the ``gpu`` benchmarks.
"""
from __future__ import annotations

import math
from collections import defaultdict, namedtuple

from lumina.results import benchmark_registry as registry
from lumina.results.filters import representative_device_label
from lumina.results.models import BenchmarkResult, MetricDirection, TestRun

SCALE = 1000.0  # a machine at the reference geomean scores this per axis.

# A metric's reference point and which way it runs. ``reference`` is the geomean of current values.
_Ref = namedtuple("_Ref", ["reference", "direction"])


def _live_references() -> dict:
    """The reference (geomean of current public values) per ``(benchmark_id, version, metric)``."""
    rows = (
        BenchmarkResult.objects.filter(run__in=TestRun.objects.public())
        .exclude(direction=MetricDirection.INFO)
        .values_list("benchmark_id", "benchmark_version", "metric", "value", "direction")
    )
    logs = defaultdict(list)
    directions = {}
    for bid, ver, metric, value, direction in rows:
        v = float(value)
        if v > 0:  # a geometric mean has no meaning through zero or a negative
            logs[(bid, ver, metric)].append(math.log(v))
            directions[(bid, ver, metric)] = direction
    return {
        key: _Ref(reference=math.exp(sum(entries) / len(entries)), direction=directions[key])
        for key, entries in logs.items()
    }


def _normalize(value, ref) -> float | None:
    """``value`` as a ratio against its reference, higher = better whichever way the metric runs."""
    reference, v = float(ref.reference), float(value)
    if reference <= 0 or v <= 0:
        return None
    ratio = v / reference if ref.direction == MetricDirection.HIGHER else reference / v
    return SCALE * ratio


def _geomean(scores) -> float | None:
    scores = [s for s in scores if s and s > 0]
    if not scores:
        return None
    return math.exp(sum(math.log(s) for s in scores) / len(scores))


def _median(values) -> float:
    values = sorted(values)
    n = len(values)
    mid = n // 2
    return values[mid] if n % 2 else (values[mid - 1] + values[mid]) / 2


def _metric_scores(rows, refs):
    """One score per ``(benchmark_id, version, metric)``, from the median value across the rows.

    Median across a model's runs, so one unusually hot or throttled submission does not set the
    model's Mark. ``rows`` are ``(benchmark_id, version, metric, value)`` tuples; returns
    ``(score, benchmark_id)`` pairs for the ones that could be normalized.
    """
    by_metric = defaultdict(list)
    for bid, ver, metric, value in rows:
        by_metric[(bid, ver, metric)].append(float(value))
    scored = []
    for (bid, ver, metric), values in by_metric.items():
        ref = refs.get((bid, ver, metric))
        if ref is None:
            continue
        score = _normalize(_median(values), ref)
        if score is not None:
            scored.append((score, bid))
    return scored


def cpu_marks(rows, refs) -> dict | None:
    """``{overall, single, multi, areas}`` for one CPU's results, or ``None`` if nothing scored.

    Each metric's benchmark is grouped by its registry axis; only ``cpu-*`` axes count here.
    """
    by_axis = defaultdict(list)
    for score, bid in _metric_scores(rows, refs):
        axis = registry.axis_of(bid)
        if axis in registry.CPU_AREAS:
            by_axis[axis].append(score)
    areas = {}
    for axis in registry.CPU_AREAS:
        area = _geomean(by_axis.get(axis, []))
        if area is not None:
            areas[axis] = round(area)
    overall = _geomean(list(areas.values()))
    if overall is None:
        return None
    return {
        "overall": round(overall),
        "single": areas.get(registry.CPU_SINGLE),
        "multi": areas.get(registry.CPU_MULTI),
        "areas": areas,
    }


def gpu_overall(rows, refs) -> int | None:
    """The GPU Mark for one card's results (``(benchmark_id, version, metric, value)`` tuples)."""
    scores = [s for s, bid in _metric_scores(rows, refs) if registry.axis_of(bid) == registry.GPU]
    overall = _geomean(scores)
    return round(overall) if overall is not None else None


def cpu_leaderboard() -> list[dict]:
    """CPU models ranked by overall Mark, each with its single-/multi-core Marks and run count.

    Keyed on ``(cpu_model, cpu_sockets)``, not the model alone: two sockets of the same part roughly
    double the multi-core result, so a dual-socket board and a single-socket one are different
    machines to rank and get their own rows.
    """
    refs = _live_references()
    rows = (
        BenchmarkResult.objects.filter(run__in=TestRun.objects.public())
        .filter(benchmark_id__in=registry.cpu_axis_ids())
        .values_list(
            "run__cpu_model", "run__cpu_sockets", "benchmark_id", "benchmark_version", "metric",
            "value", "run_id",
        )
    )
    by_key = defaultdict(list)
    runs = defaultdict(set)
    for cpu_model, sockets, bid, ver, metric, value, run_id in rows:
        if not cpu_model:
            continue
        key = (cpu_model, sockets)
        by_key[key].append((bid, ver, metric, value))
        runs[key].add(run_id)

    board = []
    for (cpu_model, sockets), model_rows in by_key.items():
        marks = cpu_marks(model_rows, refs)
        if marks:
            board.append(
                {"model": cpu_model, "sockets": sockets, "runs": len(runs[(cpu_model, sockets)]),
                 **marks}
            )
    board.sort(key=lambda entry: entry["overall"], reverse=True)
    return board


def gpu_leaderboard() -> list[dict]:
    """GPU models ranked by overall Mark. Grouped by the coalesced ``device_pci_id``/``device_model``
    key the per-benchmark leaderboards use, so a card is one row whatever backend named it."""
    refs = _live_references()
    rows = list(
        BenchmarkResult.objects.filter(run__in=TestRun.objects.public())
        .filter(benchmark_id__in=registry.gpu_axis_ids())
        .values_list(
            "device_pci_id", "device_model", "benchmark_id", "benchmark_version", "metric",
            "value", "run_id",
        )
    )
    # A card can carry its PCI id on one benchmark (clpeak resolves it) and not another
    # (cuda-bandwidth may leave it blank). Keyed on ``pci_id or model``, that split one card into a
    # PCI-keyed row beside an identical model-keyed twin. So learn each model's PCI id from wherever
    # it does appear and key every one of that model's results by it: one card, one row.
    model_pci = {}
    for pci_id, model, *_rest in rows:
        if pci_id and model:
            model_pci.setdefault(model, pci_id)

    by_gpu = defaultdict(list)
    labels = {}
    runs = defaultdict(set)
    for pci_id, model, bid, ver, metric, value, run_id in rows:
        key = pci_id or model_pci.get(model) or model
        if not key:
            continue
        by_gpu[key].append((bid, ver, metric, value))
        labels[key] = representative_device_label(labels.get(key), model)
        runs[key].add(run_id)

    board = []
    for key, gpu_rows in by_gpu.items():
        overall = gpu_overall(gpu_rows, refs)
        if overall is not None:
            board.append(
                {"key": key, "model": labels[key], "runs": len(runs[key]), "overall": overall}
            )
    board.sort(key=lambda entry: entry["overall"], reverse=True)
    return board


def _subject_marks(kind: str, samples: dict, refs: dict):
    """Marks for one subject from the compare page's ``{(benchmark_id, metric): {version: {...}}}``.

    A thin adapter onto the same ``cpu_marks``/``gpu_overall`` the leaderboard uses, so the compare
    figures are not a second formula. The axis filter inside those means a subject's off-axis
    benchmarks (memory, network) are ignored for the Mark exactly as on the leaderboard.
    """
    rows = [
        (bid, ver, metric, value)
        for (bid, metric), versions in samples.items()
        for ver, data in versions.items()
        for value in data["values"]
    ]
    if kind == "gpu":
        overall = gpu_overall(rows, refs)
        return {"overall": overall} if overall is not None else None
    return cpu_marks(rows, refs)


def marks_by_subject(kind: str, samples_by_key: dict) -> dict:
    """Composite Marks per subject key, from the metric samples the compare page already gathered.

    The reuse point for the compare page: one live-reference pass, then every subject scored through
    the leaderboard's own ``cpu_marks``/``gpu_overall``. ``kind`` is ``"cpu"`` or ``"gpu"``; a
    subject with nothing scorable maps to ``None``.
    """
    refs = _live_references()
    return {
        key: _subject_marks(kind, samples, refs) for key, samples in samples_by_key.items()
    }
