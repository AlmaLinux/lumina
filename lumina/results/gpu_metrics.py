"""Reading a clpeak metric name: which API produced it, and what it measured.

The suite names every GPU metric ``<backend>_<test>`` and stores it that way, because the raw
identifier is what makes two runs comparable and what a leaderboard is keyed on. That leaves the
translating to be done here, which is where it belongs: a stored ``vulkan_global_memory_bandwidth``
outlives whatever words we chose for it this year.

Asked for directly: GPU results should break down by clpeak's own categories and by API type. Before
this, a GPU leaderboard offered eleven raw identifiers in one flat ``<select>`` and a run page listed
them in a column of dotted lowercase, so a reader wanting the OpenCL bandwidth figure had to know the
suite's naming scheme to find it, and nothing on any page said which API a number came from.

**The API matters more than a label usually does.** A CUDA figure and an OpenCL figure for one card
are different measurements of different software stacks, not two samples of the same thing, and
whichever is faster says as much about the driver as about the silicon. So the API is never dropped
from a metric's name here; it leads it.

**Groups are derived from the words in the test's own name rather than mapped by hand**, so a test
added to the suite lands in the right group with nothing to update here. Read as words rather than as
a suffix, which was the first rule and was wrong: ``integer_compute_int8_dp`` carries its category in
the middle and was filed under "Other" next to a "Compute" section it belonged in. A tag naming no
category groups as "other" and still renders, because an unlabelled number beats a missing one.
"""
from __future__ import annotations

# clpeak's backends, spelled as the suite spells them in a metric name. The values are what a reader
# sees, so they carry the names the vendors use: nobody searches for "rocm".
BACKEND_LABELS = {
    "opencl": "OpenCL",
    "cuda": "CUDA",
    "vulkan": "Vulkan",
    "rocm": "ROCm/HIP",
    "oneapi": "oneAPI/SYCL",
}

# The twelve portable tests the suite records, in the order a reader wants them: precisions
# descending, then the memory hierarchy outward, then the host link, then latency. Dict order is the
# display order, and it is not alphabetical for a reason. Alphabetical put double precision above
# half and single, and interleaved the memory figures with the compute ones.
TAG_ORDER = (
    "single_precision_compute",
    "double_precision_compute",
    "half_precision_compute",
    "mixed_precision_compute",
    "bfloat16_compute",
    "integer_compute",
    "integer_compute_int8_dp",
    "global_memory_bandwidth",
    "local_memory_bandwidth",
    "image_memory_bandwidth",
    "transfer_bandwidth",
    "kernel_launch_latency",
)

TAG_LABELS = {
    "single_precision_compute": "Single precision",
    "double_precision_compute": "Double precision",
    "half_precision_compute": "Half precision",
    "mixed_precision_compute": "Mixed precision",
    "bfloat16_compute": "bfloat16",
    "integer_compute": "Integer",
    "integer_compute_int8_dp": "Integer, int8 dot product",
    "global_memory_bandwidth": "Global memory",
    "local_memory_bandwidth": "Local memory",
    "image_memory_bandwidth": "Image memory",
    "transfer_bandwidth": "Host transfer",
    "kernel_launch_latency": "Kernel launch",
}

# The words that name a category, whether they end the tag or sit inside it.
_GROUP_WORDS = ("compute", "bandwidth", "latency")

GROUP_LABELS = {
    "compute": "Compute",
    "bandwidth": "Bandwidth",
    "latency": "Latency",
    "other": "Other",
}

GROUP_ORDER = ("compute", "bandwidth", "latency", "other")


def split(metric: str) -> tuple[str, str]:
    """``"vulkan_global_memory_bandwidth"`` -> ``("vulkan", "global_memory_bandwidth")``.

    ``("", metric)`` when the name carries no known backend, which is every non-GPU metric in the
    database and any backend clpeak grows before this map does. Matched against the known backends
    rather than split on the first underscore, so ``events_per_sec`` is not read as an "events"
    backend measuring ``per_sec``.
    """
    name = (metric or "").strip()
    for backend in BACKEND_LABELS:
        prefix = backend + "_"
        if name.startswith(prefix) and len(name) > len(prefix):
            return backend, name[len(prefix):]
    return "", name


def group_for(tag: str) -> str:
    """Which of clpeak's categories a test belongs to, from the words in its name.

    The first category word wins, reading left to right. None of the suite's twelve tags names two,
    and picking a rule beats leaving it to dict order if one ever does.
    """
    for word in (tag or "").split("_"):
        if word in _GROUP_WORDS:
            return word
    return "other"


def tag_label(tag: str) -> str:
    """A human name for a clpeak test, derived if it is not in the map.

    Derived rather than raised over, so a test added to the suite reads as "Fp8 compute" here and
    the page keeps working until somebody gives it a better name.
    """
    known = TAG_LABELS.get(tag)
    if known:
        return known
    return (tag or "").replace("_", " ").capitalize() or tag


def is_gpu_metric(metric: str) -> bool:
    """Whether this name carries a GPU API, which is what makes the rest of this module apply."""
    return bool(split(metric)[0])


def describe(metric: str) -> dict:
    """Everything a page needs about one metric name.

    One function so the leaderboard's grouped picker, the run page's table, and the comparison rows
    cannot disagree about which API a number came from.
    """
    backend, tag = split(metric)
    group = group_for(tag)
    return {
        "metric": metric,
        "api": backend,
        "api_label": BACKEND_LABELS.get(backend, ""),
        "tag": tag,
        "tag_label": tag_label(tag),
        "group": group,
        "group_label": GROUP_LABELS.get(group, GROUP_LABELS["other"]),
        # "Vulkan single precision". The API leads because it is the part that changes what the
        # number means, and a reader scanning a list is looking for their own stack first.
        "label": (
            f"{BACKEND_LABELS[backend]} {tag_label(tag).lower()}"
            if backend else tag_label(tag)
        ),
    }


def label(metric: str) -> str:
    """The one-line name for a GPU metric, or "" for anything that is not one."""
    if not is_gpu_metric(metric):
        return ""
    return describe(metric)["label"]


def _sort_key(metric: str) -> tuple:
    """API first, then clpeak's category, then the reading order within it."""
    described = describe(metric)
    apis = list(BACKEND_LABELS)
    return (
        apis.index(described["api"]) if described["api"] in apis else len(apis),
        GROUP_ORDER.index(described["group"]) if described["group"] in GROUP_ORDER
        else len(GROUP_ORDER),
        TAG_ORDER.index(described["tag"]) if described["tag"] in TAG_ORDER else len(TAG_ORDER),
        described["tag"],
    )


def reading_order(results) -> list:
    """Benchmark rows in an order a reader can follow: by API, then by clpeak's category.

    ``BenchmarkResult.Meta`` orders alphabetically by ``benchmark_id`` then ``metric``, which for a
    GPU run interleaves the bandwidth figures with the compute ones and puts double precision above
    single. Non-GPU rows keep their existing relative order, so this is a no-op for every other
    benchmark.

    Sorted in Python rather than in the query because the order depends on ``TAG_ORDER``, which the
    database knows nothing about.
    """
    rows = list(results)
    return sorted(
        rows,
        key=lambda row: (
            row.benchmark_id,
            0 if is_gpu_metric(row.metric) else 1,
            _sort_key(row.metric) if is_gpu_metric(row.metric) else (),
        ),
    )


def grouped(metrics) -> list[dict]:
    """GPU metric names as sections a picker can render: one per API and category.

    Returns ``[]`` when nothing in the list is a GPU metric, which is how a caller decides between
    this and a flat list without asking the question twice.
    """
    gpu = [metric for metric in metrics if is_gpu_metric(metric)]
    if not gpu:
        return []
    sections: list[dict] = []
    for metric in sorted(gpu, key=_sort_key):
        described = describe(metric)
        heading = f"{described['api_label']} · {described['group_label']}"
        if not sections or sections[-1]["heading"] != heading:
            sections.append({
                "heading": heading,
                "api": described["api"],
                "api_label": described["api_label"],
                "group": described["group"],
                "group_label": described["group_label"],
                "metrics": [],
            })
        sections[-1]["metrics"].append(described)
    return sections
