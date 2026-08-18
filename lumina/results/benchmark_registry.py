"""The authoritative list of benchmarks lumina recognizes - the single source of truth.

Leaderboards are driven by *this*, not by whatever ``benchmark_id``s happen to arrive in submitted
results. A benchmark not listed here (a retired one whose old rows still sit in the database, or a
future one lumina has not been taught about) is simply never listed, ranked, or scored - no
blocklist, no data deletion. Adding a benchmark is one entry here; retiring one is ``active=False``,
which keeps its label so a run's old rows still read correctly but drops it from every leaderboard.

Each entry carries everything that defines a benchmark in one place: its label, its category, whether
it is live, and its **scoring axis** - which part of the composite Mark it feeds (see
``benchmark_scoring``). ``axis`` is ``None`` for a benchmark that is real and listed but does not
feed the CPU/GPU Mark (memory, network, scheduler): it still gets its own leaderboard, it just is not
one of the areas the Mark geomeans together.
"""
from __future__ import annotations

from dataclasses import dataclass

# Scoring axes. The ``cpu-*`` axes are the areas the CPU Mark is a geometric mean of; ``single`` and
# ``multi`` are also surfaced on their own. ``GPU`` is the GPU Mark.
CPU_SINGLE = "cpu-single"
CPU_MULTI = "cpu-multi"
CPU_COMPRESSION = "cpu-compression"
CPU_CRYPTO = "cpu-crypto"
CPU_COMPILATION = "cpu-compilation"
GPU = "gpu"

# The CPU areas, in display order, that the overall CPU Mark combines.
CPU_AREAS = (CPU_SINGLE, CPU_MULTI, CPU_COMPRESSION, CPU_CRYPTO, CPU_COMPILATION)


@dataclass(frozen=True)
class Benchmark:
    id: str
    label: str
    category: str
    axis: str | None = None  # which composite Mark this feeds; None = listed but not scored
    active: bool = True       # False = retired: keep the label for old rows, never list it


BENCHMARKS = (
    # CPU - the parts of the CPU Mark.
    Benchmark("bench.cpu.sysbench-single", "CPU, single core", "cpu", axis=CPU_SINGLE),
    Benchmark("bench.cpu.sysbench-multi", "CPU, all cores", "cpu", axis=CPU_MULTI),
    Benchmark("bench.cpu.stressng-matrix", "CPU matrix math", "cpu", axis=CPU_MULTI),
    Benchmark("bench.compress.zstd", "zstd compression", "compression", axis=CPU_COMPRESSION),
    Benchmark("bench.compress.xz", "xz compression", "compression", axis=CPU_COMPRESSION),
    Benchmark("bench.crypto.openssl-aes256gcm", "AES-256-GCM", "crypto", axis=CPU_CRYPTO),
    Benchmark("bench.crypto.openssl-sha256", "SHA-256", "crypto", axis=CPU_CRYPTO),
    Benchmark("bench.crypto.openssl-rsa4096", "RSA-4096 signing", "crypto", axis=CPU_CRYPTO),
    Benchmark("bench.compile.python", "Python build", "compilation", axis=CPU_COMPILATION),
    # GPU - the GPU Mark.
    Benchmark("bench.gpu.clpeak", "GPU compute", "gpu", axis=GPU),
    Benchmark("bench.gpu.cuda-bandwidth", "GPU transfer", "gpu", axis=GPU),
    # Listed with their own leaderboards, but not folded into a composite Mark.
    Benchmark("bench.mem.bandwidth", "Memory bandwidth", "memory"),
    Benchmark("bench.mem.latency", "Memory latency", "memory"),
    Benchmark("bench.mem.stressng-stream", "Memory stream", "memory"),
    Benchmark("bench.net.iperf3-tcp", "Network throughput", "network"),
    Benchmark("bench.net.iperf3-reverse", "Network throughput, inbound", "network"),
    Benchmark("bench.sched.stressng-switch", "Context switching", "scheduler"),
    # Retired: never listed, but old rows still resolve to a name.
    Benchmark("bench.sched.hackbench", "Scheduler, hackbench", "scheduler", active=False),
    Benchmark("bench.storage.fio-4k-randread", "Disk random read", "storage", active=False),
    Benchmark("bench.storage.fio-4k-randwrite", "Disk random write", "storage", active=False),
    Benchmark("bench.storage.fio-seq-read", "Disk sequential read", "storage", active=False),
    Benchmark("bench.storage.fio-seq-write", "Disk sequential write", "storage", active=False),
)

_BY_ID = {benchmark.id: benchmark for benchmark in BENCHMARKS}


def get(benchmark_id: str) -> Benchmark | None:
    return _BY_ID.get(benchmark_id)


def label(benchmark_id: str) -> str:
    """A human name for a benchmark id - the registry's, or one derived for an id it never knew."""
    known = _BY_ID.get(benchmark_id)
    if known:
        return known.label
    tail = (benchmark_id or "").split(".")[-1]
    return tail.replace("-", " ").replace("_", " ").capitalize() or benchmark_id


def active_benchmarks() -> list[Benchmark]:
    """Every benchmark that should be listed, in registry order."""
    return [benchmark for benchmark in BENCHMARKS if benchmark.active]


def active_ids() -> set[str]:
    return {benchmark.id for benchmark in BENCHMARKS if benchmark.active}


def is_listed(benchmark_id: str) -> bool:
    """Whether a benchmark should appear on any leaderboard at all."""
    benchmark = _BY_ID.get(benchmark_id)
    return bool(benchmark and benchmark.active)


def axis_of(benchmark_id: str) -> str | None:
    """The scoring axis of an *active* benchmark, or None (retired, unknown, or unscored)."""
    benchmark = _BY_ID.get(benchmark_id)
    return benchmark.axis if (benchmark and benchmark.active) else None


def ids_for_axis(axis: str) -> set[str]:
    return {b.id for b in BENCHMARKS if b.active and b.axis == axis}


def cpu_axis_ids() -> set[str]:
    """Active benchmark ids that feed some part of the CPU Mark."""
    return {b.id for b in BENCHMARKS if b.active and b.axis in CPU_AREAS}


def gpu_axis_ids() -> set[str]:
    return ids_for_axis(GPU)
