"""Side-by-side comparison of hardware models, averaged over every run of them.

Not run against run: the question a reader arrives with is "how does this CPU
compare to that CPU", and a single run answers it with a sample size of one. Each
column here is a hardware model, and each cell is the median across every public
run of it, with the sample count shown so a reader can weigh it.

Three rules do most of the work:

* **Socket count is part of the identity, not an average.** Averaging a 1-socket
  and a 2-socket run of the same Xeon into one all-core figure produces a number
  that describes neither machine. A 2P entry is therefore a separate column,
  labeled as such, the way a CPU comparison site lists them separately.
* **Median, not mean.** One thermally throttled or noisy-neighbor run should not
  drag a model's figure down, and with small sample counts a mean lets it.
* **A metric is only comparable within one ``benchmark_version``.** The suite
  bumps that when a measurement's meaning changes, which it has: version 1 of the
  stress-ng benchmarks published a stressor's system time under a throughput
  unit. Aggregating across versions would blend two different quantities.

Deltas are direction-aware. Memory latency is nanoseconds and lower is better, so
-8% there is an improvement while -8% on a throughput row is not.
"""
from __future__ import annotations

from lumina.results.filters import (
    GPU_BENCHMARK_ID_PREFIX,
    GPU_GROUP_KEY,
    representative_device_label,
)
from lumina.results.filters import (
    median as _median,
)
from lumina.results.filters import (
    with_gpu_group as _with_gpu_group,
)
from lumina.results.highlights import (
    CATEGORY_ORDER,
    benchmark_label,
    format_metric,
    metric_label,
)
from lumina.results.models import BenchmarkResult, MetricDirection, TestRun


def _benchmarks_of_kind(qs, kind: str):
    """Restrict a ``BenchmarkResult`` queryset to the benchmarks a compare kind is about.

    GPU benchmarks carry the ``bench.gpu.`` prefix; everything else is a platform benchmark shown
    under CPU. The same rule ``filters.group_field_for`` uses, so the picker, the comparison, and
    the leaderboard all agree on what a GPU benchmark is. This is what keeps a machine that only ran
    a GPU benchmark out of the CPU picker (its CPU has nothing to compare *as a CPU*), and a
    validated card with no GPU benchmark out of the GPU picker.
    """
    if kind == "gpu":
        return qs.filter(benchmark_id__startswith=GPU_BENCHMARK_ID_PREFIX)
    return qs.exclude(benchmark_id__startswith=GPU_BENCHMARK_ID_PREFIX)

# Four columns fit a laptop screen without horizontal scrolling, and past three
# the eye stops comparing anyway.
MAX_COMPARE = 4
MIN_COMPARE = 2

# Model strings carry spaces, parentheses, "@", and "-"; they do not carry a pipe.
KEY_SEPARATOR = "|"

SUBJECT_KINDS = {
    "cpu": {
        "field": "cpu_model",
        "label": "CPU",
        "plural": "CPUs",
        # A second socket roughly doubles an all-core score, so socket count
        # splits a model into separate comparable entities rather than being
        # averaged away.
        "split_by_sockets": True,
    },
    "gpu": {
        # A machine can carry more than one GPU, so a GPU subject is identified per card on the
        # benchmark row, not by the run's single gpu_model. ``row_field`` is a marker that the
        # subject lives on the result row (the CPU kind has none and keys off the run's cpu_model);
        # the actual grouping key is GPU_GROUP_KEY - the card's PCI id, or the device_model where no
        # id was resolved - so a card's Vulkan and OpenCL rows are one subject. Its value is unused.
        "row_field": "device_model",
        "label": "GPU",
        "plural": "GPUs",
        "split_by_sockets": False,
    },
}

DEFAULT_KIND = "cpu"




def make_key(model: str, sockets=None) -> str:
    if sockets in (None, ""):
        return str(model)
    return f"{model}{KEY_SEPARATOR}{sockets}"


def split_key(key: str) -> tuple[str, int | None]:
    model, _, sockets = str(key).rpartition(KEY_SEPARATOR)
    if not model:
        return str(key), None
    try:
        return model, int(sockets)
    except ValueError:
        return str(key), None


def parse_selection(raw: str | list | None) -> list[str]:
    """Subject keys from the request, deduplicated, order preserved.

    Order is meaningful: the first column is the baseline every delta is measured
    against, so a reader controls the comparison by choosing what comes first.
    """
    if isinstance(raw, (list, tuple)):
        parts: list[str] = []
        for item in raw:
            parts.extend(str(item).split(","))
    else:
        parts = str(raw or "").split(",")
    seen, ordered = set(), []
    for part in parts:
        value = part.strip()
        if value and value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered[:MAX_COMPARE]


def subject_options(kind: str = DEFAULT_KIND, runs=None) -> list[dict]:
    """Every hardware model with public benchmark results *of this kind*, for the picker.

    Of this kind is the point, not just any benchmark. A GPU benchmark run on a cloud instance
    records the host's CPU model, so ``with_benchmarks()`` alone offered that incidental CPU as a
    CPU to compare, with no CPU benchmark behind it and nothing to show once picked. The picker now
    lists a model only when it has a benchmark the comparison for this kind will actually display.

    Includes the sample count, because "median of 14 runs" and "median of 1 run"
    deserve different confidence and a reader can only apply that if they see it.
    """
    from django.db.models import Count, Exists, OuterRef

    kind = kind if kind in SUBJECT_KINDS else DEFAULT_KIND
    spec = SUBJECT_KINDS[kind]
    base_runs = runs if runs is not None else TestRun.objects.public()

    row_field = spec.get("row_field")
    if row_field:
        # Identity is on the benchmark row (a machine may have several of this kind), so enumerate
        # the distinct groups among this kind's benchmarks and count the runs behind each. The group
        # is the PCI-id-or-device_model key, so one card's backends collapse to one option. The
        # label is the representative device_model - the most specific name a card was seen under,
        # via the same rule the leaderboard uses so a card reads identically on both pages (a plain
        # SQL Max would pick a bare "gfx1100" ROCm codename over an AMD card's marketing name). No
        # socket split for GPUs.
        rows = _with_gpu_group(
            _benchmarks_of_kind(BenchmarkResult.objects.filter(run__in=base_runs), kind)
        ).exclude(**{GPU_GROUP_KEY: ""})
        counts = {
            entry[GPU_GROUP_KEY]: entry["runs"]
            for entry in rows.values(GPU_GROUP_KEY).annotate(runs=Count("run", distinct=True))
        }
        labels: dict[str, str] = {}
        for entry in rows.values(GPU_GROUP_KEY, "device_model").distinct():
            key = entry[GPU_GROUP_KEY]
            labels[key] = representative_device_label(labels.get(key), entry["device_model"])
        options = [
            {
                "key": make_key(key, None),
                "model": labels[key],
                "sockets": None,
                "label": _subject_label(labels[key], None),
                "runs": count,
            }
            for key, count in counts.items()
        ]
        options.sort(key=lambda option: option["label"])
        return options

    field = spec["field"]
    of_kind = _benchmarks_of_kind(
        BenchmarkResult.objects.filter(run=OuterRef("pk")), kind
    )
    base = base_runs.filter(Exists(of_kind))
    fields = [field] + (["cpu_sockets"] if spec["split_by_sockets"] else [])
    counts: dict[tuple, int] = {}
    for row in base.exclude(**{field: ""}).values(*fields):
        model = row[field]
        sockets = row.get("cpu_sockets") if spec["split_by_sockets"] else None
        counts[(model, sockets)] = counts.get((model, sockets), 0) + 1
    options = [
        {
            "key": make_key(model, sockets),
            "model": model,
            "sockets": sockets,
            "label": _subject_label(model, sockets),
            "runs": count,
        }
        for (model, sockets), count in counts.items()
    ]
    options.sort(key=lambda option: option["label"])
    return options


def _subject_label(model: str, sockets: int | None) -> str:
    """"Xeon E5-2696 v4 x2" for a dual-socket entry, plain model for one."""
    if sockets and sockets > 1:
        return f"{model} ×{sockets}"
    return str(model)


def _subject_runs(kind: str, key: str, runs=None):
    spec = SUBJECT_KINDS.get(kind) or SUBJECT_KINDS[DEFAULT_KIND]
    base = runs if runs is not None else TestRun.objects.public()
    row_field = spec.get("row_field")
    if row_field:
        # The subject lives on the benchmark row, so the runs are those with a matching group (a
        # dual-GPU machine matches every device it carries; _collect narrows to the chosen one).
        # ``key`` is the PCI-id-or-device_model group key; the label shown is the representative
        # device_model for it (same rule as the leaderboard), so a card grouped by its PCI id reads
        # as a name rather than a bare id and matches how the leaderboard names it.
        from django.db.models import Exists, OuterRef

        of_kind = _with_gpu_group(
            _benchmarks_of_kind(BenchmarkResult.objects.filter(run=OuterRef("pk")), kind)
        ).filter(**{GPU_GROUP_KEY: key})
        label = None
        for model in (
            _with_gpu_group(_benchmarks_of_kind(BenchmarkResult.objects.filter(run__in=base), kind))
            .filter(**{GPU_GROUP_KEY: key})
            .values_list("device_model", flat=True)
            .distinct()
        ):
            label = representative_device_label(label, model)
        return base.filter(Exists(of_kind)), label or key, None
    model, sockets = split_key(key) if spec["split_by_sockets"] else (key, None)
    filters = {spec["field"]: model}
    if spec["split_by_sockets"] and sockets is not None:
        filters["cpu_sockets"] = sockets
    return base.filter(**filters), model, sockets


def _gb_sort(value: str) -> int:
    """Order "8 GB" before "512 GB"; a string sort puts 512 first."""
    try:
        return int(str(value).split()[0])
    except (ValueError, IndexError):
        return 0


def _spec_rows(subjects: list) -> list[dict]:
    """What each column is, with the differing rows flagged.

    The differences are the explanation for the table below: a 723% all-core gap
    is unremarkable once the core counts are on screen.
    """
    def spanned(values, key=None) -> str:
        """A range, for quantities where the extremes are the useful summary."""
        present = sorted((v for v in values if v not in (None, "")), key=key)
        if not present:
            return ""
        if len(present) == 1:
            return str(present[0])
        # Genuinely varies across the runs behind this column; collapsing it to
        # one figure would be a claim the data does not support.
        return f"{present[0]}–{present[-1]}"

    def listed(values, limit: int | None = None) -> str:
        """A list, for labels where a range is meaningless.

        Sorting release names as strings produced "AlmaLinux 10-AlmaLinux 9",
        which is both wrong and unreadable. Capped where the list can grow
        without bound: a popular CPU turns up in dozens of machines and naming
        every one of them turns a spec row into a paragraph.
        """
        present = sorted(str(v) for v in values if v not in (None, ""))
        if limit and len(present) > limit:
            return ", ".join(present[:limit]) + f" +{len(present) - limit} more"
        return ", ".join(present)

    definitions = [
        ("Sockets", lambda s: str(s["sockets"]) if s["sockets"] else ""),
        ("Cores", lambda s: spanned(s["cores"])),
        ("Threads", lambda s: spanned(s["threads"])),
        ("Memory", lambda s: spanned(s["memory"], key=_gb_sort)),
        ("Machines tested", lambda s: listed(s["machines"], limit=3)),
        ("Runs", lambda s: str(s["run_count"])),
        ("AlmaLinux", lambda s: listed(s["releases"])),
    ]
    rows = []
    for label, getter in definitions:
        values = [getter(subject) for subject in subjects]
        if not any(values):
            continue
        rows.append({
            "label": label,
            "values": values,
            "differs": len(set(values)) > 1,
        })
    return rows


def _delta(value, baseline, direction) -> dict:
    """Percentage change from the baseline, and whether that is an improvement."""
    if baseline in (None, 0) or value is None:
        return {"percent": None, "better": None, "text": ""}
    change = (float(value) - float(baseline)) / abs(float(baseline)) * 100
    if abs(change) < 0.5:
        # Rounds to 0%. Calling that "worse" because the raw float happens to be
        # negative is noise dressed as a finding.
        return {"percent": change, "better": None, "text": "no change"}
    if direction == MetricDirection.LOWER:
        better = change < 0
    elif direction == MetricDirection.HIGHER:
        better = change > 0
    else:
        better = None  # informational metrics rank in no direction
    return {
        "percent": change,
        "better": better,
        # Signed, so it still reads as a change, with the judgement in a word
        # rather than carried by color alone.
        "text": f"{change:+.0f}%",
    }


def _collect(kind: str, keys: list, runs=None) -> list[dict]:
    """One entry per column: its runs, its hardware, and its metric samples."""
    spec = SUBJECT_KINDS.get(kind) or SUBJECT_KINDS[DEFAULT_KIND]
    row_field = spec.get("row_field")
    subjects = []
    for key in keys:
        queryset, model, sockets = _subject_runs(kind, key, runs)
        queryset = queryset.select_related("alma_release")
        found = list(queryset)
        if not found:
            continue
        samples: dict[tuple, dict] = {}
        # Only this kind's benchmarks, so a CPU comparison shows platform benchmarks and a GPU
        # comparison shows GPU benchmarks, rather than each column spilling the other kind's rows
        # from whatever else its machines happened to run.
        rows = _benchmarks_of_kind(BenchmarkResult.objects.filter(run__in=found), kind)
        if row_field:
            # A dual-GPU run is in more than one column; without this each column would show the
            # pooled numbers of both cards. Narrowing to the column's own group separates them -
            # keyed on the PCI-id-or-device_model group so a card's backends stay together. Uses the
            # loop's ``key`` (the group key), not ``model`` (which is now the display label).
            rows = _with_gpu_group(rows).filter(**{GPU_GROUP_KEY: key})
        for row in rows:
            entry = samples.setdefault((row.benchmark_id, row.metric), {})
            version = entry.setdefault(row.benchmark_version, {
                "category": row.category,
                "unit": row.unit,
                "direction": row.direction,
                "values": [],
            })
            version["values"].append(row.value)
        subjects.append({
            "key": key,
            "model": model,
            "sockets": sockets,
            "label": _subject_label(model, sockets),
            "run_count": len(found),
            "machines": {run.display_name for run in found},
            "cores": {run.cpu_cores for run in found},
            "threads": {run.cpu_threads for run in found},
            "memory": {f"{run.memory_gb} GB" for run in found if run.memory_gb},
            "releases": {
                str(run.alma_release) for run in found if run.alma_release_id
            },
            "samples": samples,
        })
    return subjects


def compare_subjects(kind: str = DEFAULT_KIND, keys: list | None = None,
                     runs=None) -> dict:
    """Aggregated metric rows and hardware specs for the selected models.

    The first subject is the baseline. Every metric any column measured gets a
    row; a column that did not measure it is blank, which is itself informative.
    """
    kind = kind if kind in SUBJECT_KINDS else DEFAULT_KIND
    subjects = _collect(kind, keys or [], runs)
    if not subjects:
        return {"kind": kind, "subjects": [], "specs": [], "groups": [],
                "metric_count": 0}

    all_keys = {key for subject in subjects for key in subject["samples"]}
    by_category: dict[str, list] = {}
    for metric_key in all_keys:
        benchmark_id, metric = metric_key
        # Pin every column to one version of the workload. The newest version any
        # column has is the one worth showing; a column with nothing at that
        # version is blank rather than silently compared across versions.
        versions = {
            version
            for subject in subjects
            for version in subject["samples"].get(metric_key, {})
        }
        version = max(versions)
        meta = next(
            subject["samples"][metric_key][version]
            for subject in subjects
            if version in subject["samples"].get(metric_key, {})
        )
        cells, baseline = [], None
        for index, subject in enumerate(subjects):
            bucket = subject["samples"].get(metric_key, {}).get(version)
            if not bucket or not bucket["values"]:
                cells.append({"present": False, "display": "", "samples": 0,
                              "delta": None})
                continue
            values = bucket["values"]
            median = _median(values)
            if index == 0:
                baseline = median
            cell = {
                "present": True,
                "value": median,
                "display": format_metric(median),
                "samples": len(values),
                "spread": (
                    f"{format_metric(min(values))}–{format_metric(max(values))}"
                    if len(values) > 1 else ""
                ),
                "is_baseline": index == 0,
                "delta": None,
            }
            if index > 0 and baseline is not None:
                cell["delta"] = _delta(median, baseline, meta["direction"])
            cells.append(cell)
        by_category.setdefault(meta["category"], []).append({
            "benchmark_id": benchmark_id,
            "metric": metric,
            "label": benchmark_label(benchmark_id),
            "unit": meta["unit"],
            "direction": meta["direction"],
            "lower_is_better": meta["direction"] == MetricDirection.LOWER,
            "higher_is_better": meta["direction"] == MetricDirection.HIGHER,
            "version": version,
            "versions": sorted(versions),
            # More than one version present across the columns: say so, because
            # the blank cells below are a version gap and not a missing test.
            "mixed_versions": len(versions) > 1,
            "cells": cells,
        })

    # A benchmark reporting several metrics would otherwise put three rows titled
    # "Memory bandwidth" on the page with different numbers in each.
    per_benchmark: dict[str, int] = {}
    for rows in by_category.values():
        for row in rows:
            per_benchmark[row["benchmark_id"]] = \
                per_benchmark.get(row["benchmark_id"], 0) + 1
    for rows in by_category.values():
        for row in rows:
            row["show_metric"] = per_benchmark[row["benchmark_id"]] > 1
            row["metric_label"] = metric_label(row["metric"])

    # The composite Marks, the same ones the overall-scores leaderboard ranks on - computed by the
    # same code (benchmark_scoring), just fed the samples already gathered above, so the compare
    # page and the leaderboard cannot disagree about a model's score.
    from lumina.results import benchmark_scoring

    marks = benchmark_scoring.marks_by_subject(
        kind, {subject["key"]: subject["samples"] for subject in subjects}
    )
    for subject in subjects:
        subject["marks"] = marks.get(subject["key"])

    ordered = [c for c in CATEGORY_ORDER if c in by_category]
    ordered += sorted(set(by_category) - set(CATEGORY_ORDER))
    groups = [
        {
            "category": category,
            "label": category.replace("_", " ").capitalize(),
            "rows": sorted(by_category[category],
                           key=lambda row: (row["label"], row["metric"])),
        }
        for category in ordered
    ]
    return {
        "kind": kind,
        "kind_label": SUBJECT_KINDS[kind]["label"],
        "subjects": subjects,
        "specs": _spec_rows(subjects),
        "groups": groups,
        "metric_count": len(all_keys),
        "has_scores": any(subject.get("marks") for subject in subjects),
    }
