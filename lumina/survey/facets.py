"""What the census counts a machine under, derived from its raw payload.

*Raw in, aggregate out.* The submission keeps the inventory it was sent, verbatim and immutable,
and everything a page shows is worked out from that. This module is where that working-out lives,
so a naming rule lives in one place and applies to every submission ever made the next time the
rollup runs.

There were three readings of the same machine before this. The rollup derived GPUs from the
payload but read ``cpu_model`` off the stored column; ingest wrote that column with a rule of its
own; and the catalog named the same card a third way. A ThinkPad P14s was "Radeon 780M" on its run
page and "HawkPoint1" in the statistics, and changing the CPU rule would have frozen ``cpu_model``
at whatever it said the day each machine reported.

The stored columns are still written, and are an **index, not a truth**: segments filter on them
(``SurveySegment.SEGMENTABLE_FIELDS`` is exactly that list) and a derived value like "Radeon 780M"
appears nowhere in the payload, so it has to be materialized to be filterable. ``index_columns``
takes the first value of the corresponding display facet wherever the two share a name, so the
cohort a segment selects and the bucket a page shows cannot disagree. They go stale when a rule
changes, which is what ``rebuild_survey_facets`` and the button beside it are for.
"""
from __future__ import annotations

from lumina.survey import devices, normalize

# Facets whose display value is the first of the values below, so a segment filtering on a column
# selects the machines the page counts in that bucket. Anything not here is an index-only column
# (``cpu_cores``) or a display-only dimension (``memory``, bucketed; ``os_version``, composed).
SHARED_FACETS = (
    "cpu_model", "cpu_vendor", "gpu_vendor", "gpu_model", "board_vendor", "arch",
    "x86_64_level", "kernel",
)


def display_facets(sub, rules: list | None = None) -> dict[str, list[str]]:
    """Every ``dimension -> values`` the census counts this machine under.

    Multi-valued because a machine genuinely can be several things at once: a workstation with an
    integrated Intel display and a discrete NVIDIA card counts under both GPU vendors, which is why
    those dimensions are in ``SurveyStat.MULTI_VALUED_DIMENSIONS`` and their shares are of machines
    rather than of mentions.

    Takes the submission rather than the payload alone: ``received_at`` decides nothing here, but
    the fallback below needs the stored columns for a payload that enumerates no devices at all.
    """
    from lumina.vendors import cpu_aliases

    inventory = sub.inventory or {}
    summary = (inventory.get("summary") or {})
    cpu = ((summary.get("cpus") or [{}])[0] or {})

    countable = devices.countable_gpus(inventory, rules)
    if countable is None:
        # The payload lists no devices at all, so there is nothing to derive from: keep what was
        # extracted at ingest rather than recording that the machine has no GPU.
        countable = [(sub.gpu_vendor, sub.gpu_model)] if sub.gpu_vendor else []

    # Derived where the payload can answer, and the stored column where it cannot. A payload
    # missing a section entirely is not the same as a machine without that part: an older
    # collector, or a bundle whose inventory was pruned, would otherwise drop out of the census
    # rather than being counted under what it did report. The GPU rule above is the same one.
    has_cpu = bool(summary.get("cpus"))
    has_board = bool(summary.get("baseboard"))
    facets: dict[str, list[str]] = {
        "cpu_model": [normalize.cpu_model(cpu.get("model") or "") if has_cpu else sub.cpu_model],
        # The brand, not the CPUID string the silicon answers with: nobody writes "GenuineIntel".
        # The table is the catalog's own, so a machine cannot be Intel in the catalog and
        # GenuineIntel in the statistics.
        "cpu_vendor": [cpu_aliases.brand(
            normalize.cpu_vendor(cpu.get("vendor") or "") if has_cpu else sub.cpu_vendor
        )],
        "cpu_sockets": [str(cpu.get("sockets") if has_cpu else sub.cpu_sockets or "")],
        "gpu_vendor": [vendor for vendor, _model in countable],
        "gpu_model": [normalize.gpu_model(model or "") for _vendor, model in countable],
        "board_vendor": [
            ((summary.get("baseboard") or {}).get("vendor") or "") if has_board
            else sub.board_vendor
        ],
        "arch": [_os(sub, "arch")],
        "x86_64_level": [_os(sub, "x86_64_level")],
        "kernel": [sub.kernel],
        "memory": [_mem_bucket(sub.memory_bytes)],
        "os_version": [f"{sub.os_major}.{sub.os_minor}"
                       if sub.os_major and sub.os_minor is not None else ""],
    }
    return {name: [v for v in values if v and v != "None"]
            for name, values in facets.items()}


def _os(sub, field: str) -> str:
    """An OS facet. Read from the column: it comes from ``environment``, which is sent beside the
    inventory and is not part of the payload this module derives from."""
    return getattr(sub, field, "") or ""


def _mem_bucket(total_bytes) -> str:
    from lumina.survey.services import _mem_bucket as bucket

    return bucket(total_bytes)


def index_columns(sub, rules: list | None = None) -> dict[str, str]:
    """The shared columns, as the display facets would have them.

    Only the facets in ``SHARED_FACETS``: the rest of the columns are either index-only
    (``cpu_cores``) or have no column at all (``memory`` is a bucket, ``os_version`` a
    composition). Written at ingest and rewritten by a rebuild, so a segment filtering on
    ``gpu_model="Radeon 780M"`` selects the machines the page counts in that bucket.

    First value where a facet is multi-valued, which is a real narrowing: a machine with two GPUs
    is filterable on one of them. The alternative is a second table, and a cohort that needs
    "has an NVIDIA GPU anywhere" is worth that table on the day somebody asks for it.
    """
    computed = display_facets(sub, rules)
    columns = {}
    for name in SHARED_FACETS:
        values = computed.get(name) or []
        columns[name] = (values[0] if values else "")[:_MAX_LENGTHS.get(name, 200)]
    return columns


# The column widths, so a rebuild cannot write a value the model will refuse.
_MAX_LENGTHS = {
    "cpu_model": 200, "cpu_vendor": 80, "gpu_vendor": 80, "gpu_model": 200,
    "board_vendor": 120, "arch": 32, "x86_64_level": 8, "kernel": 120,
}


def rebuild(queryset=None) -> int:
    """Rewrite the indexed columns of every submission from its payload. Returns how many changed.

    Never touches ``inventory`` or the identity columns: those are the raw layer and are
    append-only by construction (``SurveySubmission.save`` refuses). This only refreshes the
    derived index, which is exactly what goes stale when a naming rule is corrected.
    """
    from lumina.survey.models import SurveySubmission

    rules = _rules()
    changed: list = []
    total = 0
    for sub in (queryset if queryset is not None else SurveySubmission.objects.all()).iterator():
        columns = index_columns(sub, rules)
        if all(getattr(sub, name) == value for name, value in columns.items()):
            continue
        for name, value in columns.items():
            setattr(sub, name, value)
        changed.append(sub)
        total += 1
        # In batches, because a census is meant to grow and loading every row to rewrite a
        # handful of short strings is the kind of thing that works until it does not.
        if len(changed) >= 500:
            SurveySubmission.objects.bulk_update(changed, list(SHARED_FACETS))
            changed = []
    if changed:
        SurveySubmission.objects.bulk_update(changed, list(SHARED_FACETS))
    return total


def _rules():
    from lumina.results import exclusions

    return exclusions.active_rules()
