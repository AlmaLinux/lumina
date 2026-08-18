"""Turning raw benchmark rows into something a person reads.

The suite names its benchmarks for machines: ``bench.cpu.sysbench-multi``,
``bench.storage.fio-4k-randread``. Those are the right identifiers for a
leaderboard key and the wrong words for a public page, where the landing feed
was printing all seventeen of a run's metrics space-separated as one paragraph
of dotted identifiers.

Two jobs live here: a human label per benchmark, and picking the few numbers
worth putting in a feed row.
"""
from __future__ import annotations

from lumina.results import benchmark_registry

# The labels live in the benchmark registry now - the one authority on what benchmarks exist. This
# derived map (active plus retired ids) is kept for the handful of callers that import it directly;
# ``benchmark_label`` is the function to use.
BENCHMARK_LABELS = {benchmark.id: benchmark.label for benchmark in benchmark_registry.BENCHMARKS}

# The metrics a feed row leads with, in the order they are shown. Two of the
# three are CPU on purpose: single-core and all-core are the pair people read
# together, and reporting one without the other says little about a machine.
HEADLINE_ORDER = (
    "bench.cpu.sysbench-single",
    "bench.cpu.sysbench-multi",
    "bench.mem.bandwidth",
)

# Fallback for a run that has none of the above - a GPU-only or network-only
# benchmark run - taking one metric per category in this order.
#
# Storage is deliberately absent. The suite no longer benchmarks disks, since a
# disk figure describes whichever drive happens to be installed rather than the
# machine being certified, so this only affects results collected before that
# changed. Categories not listed sort to the end, which keeps those old rows
# readable without letting them headline a machine.
CATEGORY_ORDER = (
    "cpu", "memory", "gpu", "network",
    "crypto", "compression", "scheduler", "compilation",
)

# Which metric speaks for a category when the fallback is in play. Without this
# the pick would be alphabetical, which for cpu selects stressng-matrix over the
# sysbench numbers nobody would choose to lead with.
HEADLINE_BY_CATEGORY = {
    "cpu": "bench.cpu.sysbench-multi",
    "memory": "bench.mem.bandwidth",
    "gpu": "bench.gpu.clpeak",
    "network": "bench.net.iperf3-tcp",
    "crypto": "bench.crypto.openssl-aes256gcm",
    "compression": "bench.compress.zstd",
    "scheduler": "bench.sched.stressng-switch",
    "compilation": "bench.compile.python",
}

HEADLINE_LIMIT = 3


def benchmark_label(benchmark_id: str) -> str:
    """A human name for a benchmark id, from the registry (derived if it never knew the id)."""
    return benchmark_registry.label(benchmark_id)


# Category slugs are the suite's, and ``capfirst`` on one produces "Gpu" and "Cpu". These are the
# section headings on the leaderboard index, so an acronym rendered as a word is what somebody
# scanning the page for "GPU" reads past. Reported as GPU benchmarks not being listed.
CATEGORY_LABELS = {
    "cpu": "CPU",
    "gpu": "GPU",
    "mem": "Memory",
    "memory": "Memory",
    "crypto": "Cryptography",
    "sched": "Scheduler",
    "scheduler": "Scheduler",
    "disk": "Storage",
    "storage": "Storage",
    "net": "Network",
    "network": "Network",
}


def category_label(category: str) -> str:
    """A human heading for a benchmark category, derived if it is not in the map."""
    slug = (category or "").strip()
    known = CATEGORY_LABELS.get(slug.lower())
    if known:
        return known
    return slug.replace("-", " ").replace("_", " ").capitalize() or slug


def metric_label(metric: str) -> str:
    """"triad_bandwidth" -> "triad bandwidth".

    A benchmark can report several metrics - the bandwidth micro reports copy,
    scale, add, and triad - and labeling every one of them with just the benchmark
    name puts three identically titled rows with different numbers on the page.

    A GPU metric goes through ``gpu_metrics``, which knows that the first word of the name is the
    API that produced it. Underscore-stripping alone gave "vulkan global memory bandwidth", which
    reads as prose and buries the one part that decides what the number means.
    """
    from lumina.results.gpu_metrics import label as gpu_label

    gpu = gpu_label(metric)
    if gpu:
        return gpu
    return str(metric or "").replace("_", " ").strip()


def format_metric(value) -> str:
    """A measurement as a reader wants it: grouped digits, honest precision.

    Six decimal places is what the column stores and never what anyone wants to
    read. Precision tracks magnitude, because the tenth of an IOPS in "132830.9"
    is noise while the tenth of a nanosecond in "36.7" is the measurement.
    """
    try:
        number = float(value if value is not None else 0)
    except (TypeError, ValueError):
        return str(value)
    magnitude = abs(number)
    if magnitude >= 100:
        text = f"{number:,.0f}"
    elif magnitude >= 10:
        text = f"{number:,.1f}"
    else:
        text = f"{number:,.2f}"
    if "." in text:
        # A trailing zero claims a digit of precision that was rounded away.
        text = text.rstrip("0").rstrip(".")
    return text


def headline_metrics(rows, limit: int = HEADLINE_LIMIT) -> tuple[list, int]:
    """The few metrics that lead a feed row, and how many were left out.

    ``HEADLINE_ORDER`` first, because which numbers speak for a machine is an
    editorial decision and not something to derive. Anything it does not cover
    falls back to one metric per category, so a GPU-only or network-only run
    still leads with its own numbers instead of nothing.

    Takes an iterable of already-loaded rows, so a prefetched feed costs no
    further queries.

    Returns ``(shown, remaining)``. The count matters: a row that silently
    truncates reads as the whole story.
    """
    # Only benchmarks lumina lists headline a run: a retired or unknown benchmark's row would
    # otherwise lead a feed and link to a leaderboard that no longer exists.
    primaries = [
        row for row in rows
        if row.is_primary and benchmark_registry.is_listed(row.benchmark_id)
    ]
    by_id = {row.benchmark_id: row for row in primaries}

    shown = [by_id[name] for name in HEADLINE_ORDER if name in by_id][:limit]
    if len(shown) < limit:
        by_category: dict[str, list] = {}
        for row in primaries:
            by_category.setdefault(row.category, []).append(row)
        # Categories already speaking for this machine do not get a second turn:
        # having chosen two CPU numbers deliberately, a third would crowd out
        # whatever else the run measured.
        covered = {row.category for row in shown}
        picked = {id(row) for row in shown}
        ordered = list(CATEGORY_ORDER) + sorted(set(by_category) - set(CATEGORY_ORDER))
        for category in ordered:
            if len(shown) >= limit:
                break
            if category in covered:
                continue
            candidates = [row for row in by_category.get(category, [])
                          if id(row) not in picked]
            if not candidates:
                continue
            preferred = HEADLINE_BY_CATEGORY.get(category)
            pick = next(
                (row for row in candidates if row.benchmark_id == preferred),
                candidates[0],
            )
            shown.append(pick)
            picked.add(id(pick))
            covered.add(category)
    return shown, len(primaries) - len(shown)


def attach_headlines(runs, limit: int = HEADLINE_LIMIT) -> list:
    """Annotate runs for a feed template, which should not be doing this work."""
    runs = list(runs)
    for run in runs:
        run.headlines, run.headlines_remaining = headline_metrics(
            run.benchmarks.all(), limit=limit
        )
    return runs


__all__ = [
    "BENCHMARK_LABELS",
    "attach_headlines",
    "benchmark_label",
    "format_metric",
    "headline_metrics",
    "metric_label",
]
