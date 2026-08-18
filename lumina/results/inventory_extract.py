"""Pull the queryable fields out of a report's inventory blob.

Leaderboard filters, statistics pages, and facets all query these columns, so
they must be extracted consistently regardless of how sparse a given report
is. Every helper degrades to a blank/None rather than raising - a partial
inventory is a reviewable submission, not an ingest error.
"""
from __future__ import annotations

from typing import Any

from lumina.results.pci_names import (
    ACCELERATOR_VENDOR_IDS,
    gpu_identity,
    pci_vendor_id,
)


def _first(items: list, key: str) -> Any:
    for item in items or []:
        value = item.get(key) if isinstance(item, dict) else None
        if value:
            return value
    return None


# Kept for the ``0013`` backfill migration, which reads it. Nothing else needs it now that the
# kind is derived here rather than validated on the way in.
# "unknown" is included because ``0013_quarantine_unsupported_os`` backfills against it. The kind
# itself has only two values now; see ``SystemKind``.
VALID_SYSTEM_KINDS = {"prebuilt", "custom", "unknown"}


def first_cpu(inventory: dict) -> dict:
    """The primary CPU entry, or ``{}``.

    Its own function because the path is not obvious and is now needed in two
    places: ``collect_all`` wraps the normalized summary next to a map of raw
    artifact paths, so this lives at ``inventory.summary.cpus[0]`` rather than at
    the top level. Reading it one level too shallow returns nothing and looks like
    a machine that reported no CPU - which is exactly the mistake that hid the
    feature flags on first attempt.
    """
    cpus = ((inventory or {}).get("summary") or {}).get("cpus") or []
    first = cpus[0] if cpus else None
    return first if isinstance(first, dict) else {}


# Strings vendors leave in DMI when nobody filled the field in. Ported from the collector along
# with the classification below: "the collector shouldn't really make any decisions. It's just
# collecting and reporting."
DMI_PLACEHOLDERS = {
    "", "to be filled by o.e.m.", "default string", "system product name",
    "system manufacturer", "system version", "not specified", "not applicable",
    "no enclosure", "none", "oem", "o.e.m.", "unknown", "invalid",
    "0123456789", "12345678", "empty", "type1productconfigid", "...", "-",
}


def is_placeholder(value) -> bool:
    return not value or str(value).strip().lower() in DMI_PLACEHOLDERS


def system_kind(system: dict, baseboard: dict) -> str:
    """Classify the machine as ``prebuilt``, ``custom``, or ``unknown``.

    The question is not who screwed the machine together - people build servers, and Framework
    ships laptops as kits - it is **whether a vendor system model identifies this machine as a
    product**. "prebuilt" means the DMI System Information table names one (Dell PowerEdge R720,
    Framework Laptop 13); "custom" means it does not, and the motherboard is therefore the
    defining component.

Two things say no model exists: placeholder junk, or a table that merely mirrors the
    motherboard identity because the board maker filled it in and nobody overwrote it. Either way
    the answer is "custom": that is the fallback, and there is no third option. The mirror
    test runs against the *resolved* product name, so a vendor that stamps its machine-type code
    into both tables but also fills in a readable Version (every Lenovo laptop) is correctly read
    as a product: somebody did write a model.

    Derived here rather than at collection time. The heuristic cannot be perfect - it is a guess
    about what firmware authors meant - and a guess is exactly the kind of thing that should be
    revisable for bundles already submitted rather than frozen into each of them. A reviewer's
    correction still overrides it (``effective_system_kind``), and the verbatim DMI is in the
    bundle either way.

    One honest limit: the values read here are the collector's summary, where obvious placeholder
    junk has already been nulled. The *classification* is now revisable; recovering the exact
    original strings would mean parsing dmidecode server-side.
    """
    from lumina.results.models import SystemKind

    vendor, product = system.get("vendor"), system.get("product")
    board_vendor, board_product = baseboard.get("vendor"), baseboard.get("product")

    if is_placeholder(product):
        # No usable system model, so the machine is not claimed to be a vendor-built product -
        # which makes it a custom build, whatever its board says. There used to be a third answer
        # here, "unknown", for the case where the board named no manufacturer either: a machine
        # whose firmware was never branded could be anything, so calling it a self-build felt like
        # inventing a fact.
        #
        # It was the wrong distinction to encode as a *kind*. A machine either claims to be a
        # vendor system or it does not, and the real worry - creating a listing out of a machine
        # nothing identifies - is caught where it happens: ``create_listings_from_run`` refuses a
        # custom build with no board vendor or model, naming the field it is missing.
        return SystemKind.CUSTOM
    if (
        not is_placeholder(board_product)
        and str(product).strip().lower() == str(board_product).strip().lower()
        and str(vendor or "").strip().lower() == str(board_vendor or "").strip().lower()
    ):
        return SystemKind.CUSTOM
    return SystemKind.PREBUILT


def extract(inventory: dict) -> dict:
    """Return denormalized TestRun column values from ``inventory``."""
    summary = (inventory or {}).get("summary") or {}
    system = summary.get("system") or {}
    baseboard = summary.get("baseboard") or {}
    memory = summary.get("memory") or {}
    gpus = summary.get("gpus") or []

    cpu = first_cpu(inventory)
    gpu = _primary_gpu(gpus)

    total_bytes = memory.get("total_bytes") or 0
    try:
        memory_mb = int(total_bytes) // (1024 * 1024) or None
    except (TypeError, ValueError):
        memory_mb = None

    # Derived here, not read from the report. The collector used to classify and write
    # ``summary.system.kind``; a run ingested before this reports one, and it is ignored - the
    # rule lives in one place so a correction reaches every bundle.
    kind = system_kind(system, baseboard)

    return {
        "cpu_model": _clean(cpu.get("model"), 200),
        "cpu_vendor": _clean(cpu.get("vendor"), 80),
        "cpu_cores": _as_int(cpu.get("cores")),
        # cpu_cores is the machine's total across sockets, so the socket count
        # is what makes an all-core score interpretable rather than just large.
        "cpu_sockets": _as_int(cpu.get("sockets")),
        "cpu_threads": _as_int(cpu.get("threads")),
        "memory_mb": memory_mb,
        **_memory_summary(memory.get("dimms")),
        # Through ``gpu_identity``, because the collector no longer decides which of lspci's
        # names is the product's. Reading ``model`` directly left this column empty for every
        # bundle written after that change - and it is what the run pages and the leaderboards
        # display.
        "gpu_model": _clean(gpu_identity(gpu)[1], 200),
        "gpu_driver": _gpu_driver(gpu),
        "system_kind": kind,
        "system_vendor": _clean(system.get("vendor"), 120),
        "system_product": _clean(system.get("product"), 200),
        # Absent from 1.0 and early 1.1 reports; only vendors who separate the
        # two send it at all.
        "system_model_number": _clean(system.get("model_number"), 120),
        "board_vendor": _clean(baseboard.get("vendor"), 120),
        "board_model": _clean(baseboard.get("product"), 200),
    }


def _memory_summary(dimms: Any) -> dict:
    """Populated-DIMM count, module type, and clock, from the per-module detail.

    The full list stays in ``inventory``; these three are what a filter or a
    statistic can query. Mixed configurations are real, so the type reported is
    the most common one and the speed is the lowest: modules of different rated
    speeds all clock down to the slowest, which is the speed the machine
    actually ran the benchmark at.
    """
    modules = [d for d in (dimms or []) if isinstance(d, dict)]
    if not modules:
        return {"memory_dimm_count": None, "memory_type": "",
                "memory_speed_mts": None}
    types = [str(d.get("type")).strip() for d in modules if d.get("type")]
    speeds = [_as_int(d.get("speed_mts")) for d in modules]
    speeds = [s for s in speeds if s]
    return {
        "memory_dimm_count": len(modules),
        "memory_type": _clean(max(set(types), key=types.count) if types else "", 32),
        "memory_speed_mts": min(speeds) if speeds else None,
    }


def _primary_gpu(gpus: list) -> dict:
    """Prefer a discrete GPU with an identified driver over integrated/BMC display adapters, which
    are present on nearly every server and would otherwise dominate the statistics.

    On the PCI vendor id. This compared ``g.get("vendor")`` against three tokens, and there is no
    such key: the collector stopped flattening it so a naming rule that turned out wrong could be
    corrected for bundles already submitted. The discrete list was therefore always empty and this
    fell through to "whatever lspci listed first", which on a server with a Matrox BMC adapter is
    the Matrox.

    Not on the resolved *name* either, which was the first fix and also wrong: ``gpu_identity``
    returns pci.ids' spelling, so the name is "NVIDIA Corporation" rather than "NVIDIA", and AMD
    ships as "Advanced Micro Devices, Inc. [AMD/ATI]". The id is four hex digits and does not get
    reworded.
    """
    candidates = [g for g in gpus if isinstance(g, dict)]
    if not candidates:
        return {}
    discrete = [
        g for g in candidates
        if g.get("driver") and pci_vendor_id(g) in ACCELERATOR_VENDOR_IDS
    ]
    preferred = [g for g in discrete if g.get("runtime")]
    return (preferred or discrete or candidates)[0]


def _gpu_driver(gpu: dict) -> str:
    driver = gpu.get("driver") or ""
    version = gpu.get("driver_version") or ""
    if driver and version:
        return _clean(f"{driver} {version}", 120)
    return _clean(driver or version, 120)


def _clean(value: Any, max_length: int) -> str:
    if value is None:
        return ""
    return str(value).strip()[:max_length]


def _as_int(value: Any) -> int | None:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


ALMALINUX_OS_ID = "almalinux"


def host_os_id(environment: dict) -> str:
    """``environment.os.id`` - the distribution the run was performed on.

    Lowercased, because ``/etc/os-release`` is only conventionally lowercase and
    a report saying ``AlmaLinux`` should not be treated as a different OS.
    """
    return str(((environment or {}).get("os") or {}).get("id") or "").strip().lower()


def is_almalinux(environment: dict) -> bool:
    """Whether this report came from AlmaLinux.

    Exact match on ``ID``. ``ID_LIKE`` is not consulted: every RHEL rebuild
    carries ``ID_LIKE="rhel centos fedora"``, so honouring it would admit exactly
    the distributions this test exists to separate. An absent or unreadable
    os-release yields "", which is not AlmaLinux - "cannot tell" must not mean
    "supported", or a stripped image is the way around the check.
    """
    return host_os_id(environment) == ALMALINUX_OS_ID


def parse_release(environment: dict) -> tuple[int | None, int | None]:
    """(major, minor) from environment.os.version_id, e.g. "9.6" -> (9, 6).

    **Only for AlmaLinux reports.** Every RHEL rebuild numbers its releases the
    same way, so Rocky 9.6 and RHEL 9.6 both parse to (9, 6) and would match
    ``AlmaLinuxRelease(major=9)`` exactly as an AlmaLinux run does. Returning the
    numbers for a non-AlmaLinux report is how a Rocky run would end up recorded as
    proof of AlmaLinux 9 support, so it returns nothing instead and the caller has
    no release to bind.
    """
    if not is_almalinux(environment):
        return None, None
    return parse_version(environment)


def parse_version(environment: dict) -> tuple[int | None, int | None]:
    """The raw ``version_id`` numbers, whatever distribution reported them.

    Separate from ``parse_release`` so the OS gate cannot be bypassed by
    accident: everything on the ingest path wants the gated version, and the one
    caller that legitimately wants the numbers from a non-AlmaLinux report is a
    reviewer overriding a quarantine, having decided the OS was misreported.
    """
    version = ((environment or {}).get("os") or {}).get("version_id") or ""
    parts = str(version).split(".")
    try:
        major = int(parts[0])
    except (IndexError, ValueError):
        return None, None
    try:
        minor = int(parts[1])
    except (IndexError, ValueError):
        minor = None
    return major, minor
