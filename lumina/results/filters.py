"""Shared filters for results, leaderboards, and statistics.

Following the ``filter_listings`` convention: one implementation used by both
the HTML views and the JSON API so the two surfaces cannot drift.

Every function here starts from ``TestRun.objects.public()`` - embargoed and
unreviewed runs never reach a public surface through this module.
"""
from __future__ import annotations

from typing import Any

from django.db.models import Count, Max, QuerySet

from lumina.results.models import BenchmarkResult, MetricDirection, RunType, TestRun


def _majority_direction(qs: QuerySet) -> str:
    """The direction most of a metric's rows agree on.

    Which way a leaderboard sorts, lower-is-better or higher-is-better, is a fact about the metric,
    but nothing in lumina defines it: the suite that produced the bundle knows, and each row simply
    carries what its bundle said. Taking the first row's direction let one submitter invert the
    ranking for everyone by labelling their own row the wrong way. A majority across the metric's
    approved rows cannot be flipped by a single submission, which is the property that matters here.

    Aggregated in the database rather than by pulling every row's direction into Python. Ties fall to
    whichever the database returns first, which is harmless: a metric evenly split on direction has
    no agreed answer to protect.
    """
    top = (
        qs.values("direction")
        .annotate(n=Count("id"))
        .order_by("-n")
        .values_list("direction", flat=True)
        .first()
    )
    return top or MetricDirection.INFO

FACET_FIELDS = {
    "cpu": "run__cpu_model",
    "board": "run__board_model",
    # Per-device on the benchmark row, not the run's single gpu_model: a machine with two GPUs
    # produces a result row per card, so faceting on the row is what lets each model be found.
    "gpu": "device_model",
    "gpu_driver": "run__gpu_driver",
    "vendor": "run__system_vendor",
    "alma": "run__alma_release__major",
    # Socket count belongs beside the CPU model, not buried in the run detail.
    # An all-core score doubles with a second socket, so ranking a 2P box
    # against a 1P box on model alone compares two different machines.
    "sockets": "run__cpu_sockets",
    "memory_type": "run__memory_type",
    "memory_speed": "run__memory_speed_mts",
}

RUN_FACET_FIELDS = {
    "cpu": "cpu_model",
    "board": "board_model",
    "gpu": "gpu_model",
    "gpu_driver": "gpu_driver",
    "vendor": "system_vendor",
    "alma": "alma_release__major",
    "sockets": "cpu_sockets",
    "memory_type": "memory_type",
    "memory_speed": "memory_speed_mts",
}

# Facets over integer columns. ``exclude(field="")`` is valid for a CharField
# and raises on an integer one, so which is which has to be known rather than
# guessed from the field name.
NUMERIC_FACETS = frozenset({"alma", "sockets", "memory_speed"})


def _get(params: dict, key: str) -> str:
    value = params.get(key)
    if isinstance(value, (list, tuple)):
        return value[0] if value else ""
    return value or ""


def benchmark_catalog() -> list[dict[str, Any]]:
    """Benchmarks that have at least one public result, newest version first."""
    rows = (
        BenchmarkResult.objects.filter(run__in=TestRun.objects.public())
        .values("benchmark_id", "category")
        .annotate(runs=Count("run", distinct=True), latest=Max("benchmark_version"))
        .order_by("category", "benchmark_id")
    )
    return list(rows)


def latest_version_for(benchmark_id: str) -> str | None:
    return (
        BenchmarkResult.objects.filter(
            run__in=TestRun.objects.public(), benchmark_id=benchmark_id
        )
        .aggregate(latest=Max("benchmark_version"))
        .get("latest")
    )


def default_metric_for(benchmark_id: str, version: str | None) -> str | None:
    """The primary metric if the suite marked one, else the first alphabetically."""
    qs = BenchmarkResult.objects.filter(
        run__in=TestRun.objects.public(), benchmark_id=benchmark_id
    )
    if version:
        qs = qs.filter(benchmark_version=version)
    primary = qs.filter(is_primary=True).values_list("metric", flat=True).first()
    if primary:
        return primary
    return qs.order_by("metric").values_list("metric", flat=True).first()


def filter_leaderboard(
    *, benchmark_id: str, params: dict | None = None
) -> QuerySet[BenchmarkResult]:
    """Ranked public results for one comparable (benchmark, version, metric).

    Ranking across benchmark versions would compare different workloads, so
    the version is always pinned - to the requested one, or the newest with
    public data.
    """
    params = params or {}
    version = _get(params, "version") or latest_version_for(benchmark_id)
    metric = _get(params, "metric") or default_metric_for(benchmark_id, version)

    qs = BenchmarkResult.objects.filter(
        run__in=TestRun.objects.public(), benchmark_id=benchmark_id
    ).select_related("run", "run__alma_release", "run__submitter")
    if version:
        qs = qs.filter(benchmark_version=version)
    if metric:
        qs = qs.filter(metric=metric)

    for param, field in FACET_FIELDS.items():
        value = _get(params, param)
        if value:
            qs = qs.filter(**{field: value})

    # ?cpu_family=/gpu_family= narrows to every model the family matches.
    # Resolved to an IN list because the patterns are regexes the database
    # cannot index against.
    for param, (dimension, kind_name) in FAMILY_GROUPS.items():
        wanted = _get(params, param)
        if not wanted:
            continue
        from lumina.hardware.models import ComponentKind
        from lumina.results.component_match import family_for_model

        model_field = GROUP_FIELDS[dimension][0]
        present = (
            qs.exclude(**{model_field: ""})
            .values_list(model_field, flat=True)
            .distinct()
        )
        kind = ComponentKind(kind_name)
        matching = [
            model for model in present
            if (fam := family_for_model(model, kind)) is not None
            and fam.name == wanted
        ]
        qs = qs.filter(**{f"{model_field}__in": matching})

    direction = _majority_direction(qs)
    order = "value" if direction == MetricDirection.LOWER else "-value"
    return qs.order_by(order, "run__received_at")


def leaderboard_families(benchmark_id: str, version: str | None,
                         dimension: str = "cpu") -> list[str]:
    """Curated families present among this benchmark's results.

    Options for the family *filter*. Patterns are resolved at read time, so a
    family curated in the admin becomes filterable immediately without touching
    stored rows.
    """
    from lumina.hardware.models import ComponentKind
    from lumina.results.component_match import group_models_by_family

    field = GROUP_FIELDS.get(dimension, GROUP_FIELDS["cpu"])[0]
    base = BenchmarkResult.objects.filter(
        run__in=TestRun.objects.public(), benchmark_id=benchmark_id
    )
    if version:
        base = base.filter(benchmark_version=version)
    models = {
        model for model in base.values_list(field, flat=True).distinct() if model
    }
    if not models:
        return []
    grouped = group_models_by_family(sorted(models), ComponentKind(dimension))
    # A model matching no curated family is keyed by itself; those are not
    # families and would pad the list with every unmatched part.
    return sorted(name for name, matched in grouped.items()
                  if name not in models or len(matched) > 1)


def leaderboard_facets(benchmark_id: str, version: str | None) -> dict[str, list]:
    """Distinct facet values available for this benchmark's public results."""
    base = BenchmarkResult.objects.filter(
        run__in=TestRun.objects.public(), benchmark_id=benchmark_id
    )
    if version:
        base = base.filter(benchmark_version=version)
    facets = {}
    for param, field in FACET_FIELDS.items():
        qs = base.exclude(**{f"{field}__isnull": True})
        if param not in NUMERIC_FACETS:
            qs = qs.exclude(**{field: ""})
        values = qs.values_list(field, flat=True).distinct().order_by(field)
        facets[param] = [v for v in values if v not in ("", None)]
    facets["metrics"] = list(
        base.values_list("metric", flat=True).distinct().order_by("metric")
    )
    facets["versions"] = list(
        base.model.objects.filter(
            run__in=TestRun.objects.public(), benchmark_id=benchmark_id
        )
        .values_list("benchmark_version", flat=True)
        .distinct()
        .order_by("-benchmark_version")
    )
    return facets


# Which hardware dimension a benchmark is naturally grouped by: GPU
# benchmarks vary with the graphics card, everything else with the CPU.
# A benchmark belongs to the GPU kind iff its id carries this prefix; everything else is a platform
# benchmark shown under CPU. One rule, used by the leaderboard grouping and by the compare subject
# picker, so the two cannot disagree about what counts as a GPU benchmark.
GPU_BENCHMARK_ID_PREFIX = "bench.gpu."


def group_field_for(benchmark_id: str) -> str:
    """Default grouping dimension: the model.

    Family was the default until it became clear what it produces. A family's
    median is whatever mix of its models people happened to run - twelve runs of
    the cheapest SKU and one of the flagship - so "Ryzen 7000 series" ranks by
    submission habits rather than by hardware. Filtering by a family is still
    useful, because narrowing to a generation and then comparing its models is a
    real question; ranking families against each other is not.
    """
    return "gpu" if benchmark_id.startswith(GPU_BENCHMARK_ID_PREFIX) else "cpu"


GROUP_FIELDS = {
    "cpu": ("run__cpu_model", "CPU model"),
    "sockets": ("run__cpu_sockets", "Socket count"),
    # Keyed on the benchmark row's device_model, not the run's single gpu_model, so a dual-GPU
    # machine's two cards land in their own groups. Rows with a blank device_model (non-GPU
    # benchmarks, or pre-per-device reports) are skipped by leaderboard_groups' empty-key guard.
    "gpu": ("device_model", "GPU model"),
    "system": ("run__listing_system__name", "System"),
    "board": ("run__board_model", "Motherboard"),
}

# ``?cpu_family=`` / ``?gpu_family=`` narrow the results to every model a curated
# family matches. Filters, not grouping keys: the patterns are resolved at read
# time to an IN list, since a database cannot index against a regex.
FAMILY_GROUPS = {
    "cpu_family": ("cpu", "cpu"),   # (dimension whose model field to match, ComponentKind)
    "gpu_family": ("gpu", "gpu"),
}


def _median(values: list) -> float:
    """Median of a non-empty sorted-able list.

    Computed in Python on purpose: MariaDB has no median/percentile
    aggregate, and result sets per benchmark are small enough that pulling
    the values costs less than a portable SQL workaround.
    """
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (float(ordered[mid - 1]) + float(ordered[mid])) / 2


def leaderboard_groups(
    *, benchmark_id: str, params: dict | None = None, group_by: str | None = None
) -> dict:
    """Aggregate a benchmark's public results by a hardware dimension.

    Individual runs answer "who posted the fastest number"; grouping answers
    "how does this CPU/GPU perform", which is what a reader comparing
    hardware actually wants. Each group reports its median (robust against a
    single tuned outlier), its best, and how many runs back it.
    """
    params = params or {}
    group_by = group_by or group_field_for(benchmark_id)
    field, label = GROUP_FIELDS.get(group_by, GROUP_FIELDS["cpu"])

    rows = filter_leaderboard(benchmark_id=benchmark_id, params=params)
    lower_better = _majority_direction(rows) == MetricDirection.LOWER

    buckets: dict[str, list] = {}
    unit = ""
    for result in rows.values(field, "value", "unit"):
        key = result[field]
        if key in (None, ""):
            continue  # a run with no value for this dimension cannot be grouped
        buckets.setdefault(key, []).append(result["value"])
        unit = unit or result["unit"]

    groups = []
    for key, values in buckets.items():
        best = min(values) if lower_better else max(values)
        groups.append({
            "key": key,
            "median": _median(values),
            "best": float(best),
            "runs": len(values),
        })
    groups.sort(key=lambda g: g["median"], reverse=not lower_better)

    # Bar length is always proportional to the value; ranking carries the
    # direction, and the template states "lower is better" explicitly so a
    # long bar is never mistaken for a good one.
    ceiling = max((g["median"] for g in groups), default=0) or 1
    for rank, group in enumerate(groups, start=1):
        group["rank"] = rank
        group["percent"] = round(group["median"] / ceiling * 100, 1)

    return {
        "groups": groups,
        "group_by": group_by,
        "group_label": label,
        "unit": unit,
        "lower_better": lower_better,
    }


def filter_runs(params: dict | None = None) -> QuerySet[TestRun]:
    """Public runs, filtered by run type, release, and hardware attributes."""
    params = params or {}
    qs = TestRun.objects.public().select_related(
        "alma_release", "submitter", "listing_system", "listing_system__vendor"
    )
    run_type = _get(params, "run_type")
    if run_type in {t.value for t in RunType}:
        qs = qs.filter(run_type=run_type)
    for param, field in RUN_FACET_FIELDS.items():
        value = _get(params, param)
        if value:
            qs = qs.filter(**{field: value})
    system = _get(params, "system")
    if system:
        qs = qs.filter(listing_system__slug=system)
    return qs


def hardware_stats(limit: int = 10) -> dict[str, Any]:
    """Aggregates over public runs for the statistics page."""
    runs = TestRun.objects.public().filter(target_type="hardware")

    def top(field: str) -> list[dict]:
        rows = (
            runs.exclude(**{field: ""})
            .values(field)
            .annotate(count=Count("id"))
            .order_by("-count", field)[:limit]
        )
        return [{"label": row[field], "count": row["count"]} for row in rows]

    memory_buckets: dict[str, int] = {}
    for value in runs.exclude(memory_mb=None).values_list("memory_mb", flat=True):
        memory_buckets[_memory_bucket(value)] = memory_buckets.get(_memory_bucket(value), 0) + 1

    return {
        "total_runs": runs.count(),
        "total_systems": runs.exclude(system_product="")
        .values("system_vendor", "system_product")
        .distinct()
        .count(),
        "cpu_models": top("cpu_model"),
        "cpu_vendors": top("cpu_vendor"),
        "motherboards": top("board_model"),
        "system_vendors": top("system_vendor"),
        "gpu_models": top("gpu_model"),
        "gpu_drivers": top("gpu_driver"),
        "releases": list(
            runs.exclude(alma_release=None)
            .values("alma_release__major")
            .annotate(count=Count("id"))
            .order_by("-alma_release__major")
        ),
        "memory_buckets": sorted(
            ({"bucket": k, "count": v} for k, v in memory_buckets.items()),
            key=lambda row: _bucket_sort_key(row["bucket"]),
        ),
    }


_MEMORY_BUCKETS = [
    (8 * 1024, "< 8 GB"),
    (16 * 1024, "8-16 GB"),
    (32 * 1024, "16-32 GB"),
    (64 * 1024, "32-64 GB"),
    (128 * 1024, "64-128 GB"),
    (256 * 1024, "128-256 GB"),
    (512 * 1024, "256-512 GB"),
    (1024 * 1024, "512 GB-1 TB"),
]


def _memory_bucket(memory_mb: int) -> str:
    for ceiling, label in _MEMORY_BUCKETS:
        if memory_mb < ceiling:
            return label
    return "≥ 1 TB"


def _bucket_sort_key(label: str) -> int:
    order = [lbl for _, lbl in _MEMORY_BUCKETS] + ["≥ 1 TB"]
    return order.index(label) if label in order else len(order)
