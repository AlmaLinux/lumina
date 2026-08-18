"""Reading the survey rollup for publication: shares, movement, and geometry.

Everything the statistics page shows is computed here, from ``SurveyStat`` alone. The
page is a Steam-hardware-survey-shaped read of the census: what the fleet looks like in
one period, and which way each part of it is moving.

Three deliberate choices live in this module.

**Periods are not summed.** A year is its own rollup row, not the total of its months,
because dedup happens inside a period: a machine reporting every month is one machine in
each of those months and one machine in the year. Adding months would count it twelve
times. So a granularity is chosen and everything on the page stays inside it.

**Shares, not counts, drive the charts.** A census that grows changes every count in the
same direction, which makes a raw-count trend a chart of survey adoption rather than of
hardware. Share answers the question the page is actually asking.

**Chart geometry is computed here, not in the template.** A template that does
arithmetic in ``{% widthratio %}`` cannot be tested and quietly renders nonsense when a
series is empty or flat. These functions hand the template finished coordinates.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from lumina.survey.models import SurveySegment, SurveyStat

# The published dimensions, in reading order: what the machine runs, then what it is.
DIMENSION_ORDER = [
    "os_version", "arch", "x86_64_level", "cpu_vendor", "cpu_model", "cpu_sockets",
    "memory", "gpu_vendor", "gpu_model", "board_vendor", "kernel",
]
DIMENSION_LABELS = {
    "os_version": "AlmaLinux version",
    "arch": "Architecture",
    "x86_64_level": "x86-64 feature level",
    "cpu_vendor": "CPU vendor",
    "cpu_model": "CPU model",
    "cpu_sockets": "CPU sockets",
    "memory": "Memory",
    "gpu_vendor": "GPU vendor",
    "gpu_model": "GPU model",
    "board_vendor": "Board vendor",
    "kernel": "Kernel",
}

# Dimensions a small number of brands dominate, so a multi-series trend line is readable.
# Everything else gets a sparkline per row instead: a line chart of fifteen CPU models is
# unreadable at any size, and the palette honestly supports three series plus a remainder.
TREND_DIMENSIONS = ["cpu_vendor", "gpu_vendor", "arch", "os_version"]

# Named series a trend chart draws before folding the rest into "Other". Three, because
# that is what the AlmaLinux brand hues support: with one step per hue family and the
# neutral remainder counted as a series, three chromatic slots is the largest set whose
# every pair clears the colour-vision-deficiency separation floor. A fourth would have to
# be an invented hue or a confusable one. See lumina-charts.css.
TREND_SERIES = 3
OTHER_LABEL = "Other"

_ROWS_PER_SECTION = 15
_SPARK_PERIODS = 12

# The at-a-glance band at the top of the page: the single most common answer in each of
# a few dimensions. A headline figure needs no chart, and putting the four answers most
# people came for above the fold is what stops the page reading as a wall of bar rows.
HEADLINE_DIMENSIONS = ["cpu_vendor", "gpu_vendor", "memory", "os_version"]


@dataclass
class Bucket:
    """One row of a distribution: a bucket, its share, and which way it is moving."""

    label: str
    count: int
    share: float                      # percent of machines in this dimension
    delta: float | None = None        # percentage points against the previous period
    spark: str = ""                   # polyline points for this bucket's own history

    @property
    def has_delta(self) -> bool:
        """Whether there is a previous period to compare against at all."""
        return self.delta is not None

    @property
    def direction(self) -> str:
        """"up", "down", "flat", or "none", so a caller never restates the threshold.

        "none" is not "flat": a first period has nothing to compare against, and
        reporting that as no change would claim a measurement nobody made.
        """
        if self.delta is None:
            return "none"
        if abs(self.delta) < 0.05:
            return "flat"
        return "up" if self.delta > 0 else "down"


@dataclass
class Series:
    """One line of a trend chart."""

    label: str
    slot: int                         # 0-2 for a named series, 3 for the remainder
    points: str = ""                  # "x,y x,y ..." in the viewBox below
    last: float = 0.0
    label_y: float = 0.0              # where a direct label sits, if it gets one


@dataclass
class Trend:
    """A multi-series share-over-time chart, with the table that stands in for it."""

    dimension: str
    label: str
    periods: list[str] = field(default_factory=list)
    series: list[Series] = field(default_factory=list)
    y_max: float = 100.0
    grid: list[dict] = field(default_factory=list)
    x_ticks: list[dict] = field(default_factory=list)
    # Percent per series per period, oldest first: the same numbers the lines are drawn
    # from. Present because a line chart is not readable by a screen reader and because
    # somebody will want the figure rather than the picture.
    table: list[dict] = field(default_factory=list)

    # --- geometry, so the template never restates the padding -----------------
    #
    # These were literals in the markup, which is how the y-axis label came to be clipped:
    # PAD_L moved and the "x" it was drawn at did not. Anything positioned relative to the
    # plot box is derived here instead.

    @property
    def plot_left(self) -> float:
        return PAD_L

    @property
    def plot_right(self) -> float:
        return VIEW_W - PAD_R

    @property
    def y_label_x(self) -> float:
        """Right-aligned just clear of the axis."""
        return PAD_L - 4

    @property
    def x_label_y(self) -> float:
        """Below the baseline, clear of the descenders on the axis line."""
        return VIEW_H - PAD_B + 14

    @property
    def end_label_x(self) -> float:
        """Just past the last point, in the right-hand gutter PAD_R reserves."""
        return VIEW_W - PAD_R + 6

    @property
    def has_other(self) -> bool:
        """Whether anything was actually folded into the remainder.

        A dimension with three or fewer buckets names all of them and has no Other, so
        the page must not announce one. Reading it off the series list rather than the
        bucket count keeps the two from disagreeing.
        """
        return any(s.label == OTHER_LABEL for s in self.series)

    @property
    def named(self) -> int:
        """How many buckets are named individually, remainder excluded."""
        return len(self.series) - (1 if self.has_other else 0)


# The one viewBox every chart in this module draws into. Fixed rather than responsive so
# the geometry is arithmetic here and scaling is the browser's job.
VIEW_W = 720.0
VIEW_H = 220.0
# Wide enough for the widest y-axis label, which is "100%" at the 16-unit font set in
# lumina-charts.css. At 34 the first chart to reach a full-height axis rendered it as
# "00%", clipped against the left edge of the viewBox.
PAD_L = 46.0
PAD_R = 58.0    # room for a direct label at the end of a line
PAD_T = 10.0
PAD_B = 22.0


def _plot_w() -> float:
    return VIEW_W - PAD_L - PAD_R


def _plot_h() -> float:
    return VIEW_H - PAD_T - PAD_B


def is_month(period: str) -> bool:
    return "-" in period


def available_periods(segment: str = "") -> dict[str, list[str]]:
    """Published periods for one cohort, newest first, split by granularity.

    Scoped to the segment because a cohort can have no data in a period the whole fleet
    does, and offering that period would show an empty page with a period selected.
    """
    everything = list(
        SurveyStat.objects.filter(segment=segment).order_by("-period")
        .values_list("period", flat=True).distinct()
    )
    return {
        "month": [p for p in everything if is_month(p)],
        "year": [p for p in everything if not is_month(p)],
    }


def previous_period(period: str, known: list[str]) -> str | None:
    """The period immediately before this one *that has data*.

    Not "the month before", deliberately: a census with a gap in it would otherwise
    compare September against an empty August and report that everything collapsed.
    ``known`` is newest first.
    """
    same = [p for p in known if is_month(p) == is_month(period)]
    if period not in same:
        return None
    index = same.index(period)
    return same[index + 1] if index + 1 < len(same) else None


def _totals(rows) -> dict[tuple[str, str], int]:
    """Machines counted per (period, dimension), which is what a share divides by."""
    totals: dict[tuple[str, str], int] = {}
    for row in rows:
        key = (row.period, row.dimension)
        totals[key] = totals.get(key, 0) + row.count
    return totals


def _share(count: int, total: int) -> float:
    return (100.0 * count / total) if total else 0.0


def _spark_points(history: list[float]) -> str:
    """A sparkline for one bucket's share history, oldest first.

    Scaled to its own maximum rather than to 100: a bucket that moves between 2% and 4%
    is a flat line against a full axis, and the row already states the absolute share.

    Empty until there are at least two periods to compare. A single period was drawn as a
    flat two-point line, on the reasoning that a row should never be blank, which was
    wrong: a horizontal dash beside every row reads as a trend that happens to be flat,
    which is a claim about history that the survey has not yet earned.
    """
    if len(history) < 2:
        return ""
    top = max(history) or 1.0
    step = 100.0 / (len(history) - 1)
    return " ".join(
        f"{i * step:.1f},{28.0 - 24.0 * (value / top):.1f}"
        for i, value in enumerate(history)
    )


def distribution(
    period: str,
    *,
    tier: str = SurveyStat.TIER_VERIFIED,
    periods: list[str] | None = None,
    limit: int = _ROWS_PER_SECTION,
    segment: str = "",
) -> list[dict]:
    """Every published dimension for one period, ranked, with movement and sparklines.

    One query for the period, one for the sparkline window, rather than a query per
    dimension or per row: the page renders eleven dimensions of up to fifteen rows and
    the naive shape of this was 165 queries.
    """
    known = periods if periods is not None else available_periods(segment)[
        "month" if is_month(period) else "year"
    ]
    window = [p for p in known if is_month(p) == is_month(period)]
    window = window[window.index(period):][:_SPARK_PERIODS] if period in window else [period]
    previous = previous_period(period, known)

    # Ties break on the bucket name, not on whatever order the database returns. Server
    # GPUs produce four-way ties at a count of one constantly, and without this the rows
    # below the tie reshuffle between page loads and a trend changes which series it names.
    rows = list(
        SurveyStat.objects.filter(segment=segment, period__in=window, tier_scope=tier)
        .order_by("dimension", "-count", "bucket")
    )
    # Only when it is not already in the window above. Fetching it twice would count its
    # rows twice in ``_totals``, halving every share in the period being compared against
    # and reporting movement that never happened.
    if previous and previous not in window:
        rows += list(
            SurveyStat.objects.filter(segment=segment, period=previous, tier_scope=tier)
        )
    totals = _totals(rows)

    # (dimension, bucket) -> {period: share}, so a row's history is a lookup.
    history: dict[tuple[str, str], dict[str, float]] = {}
    for row in rows:
        share = _share(row.count, totals.get((row.period, row.dimension), 0))
        history.setdefault((row.dimension, row.bucket), {})[row.period] = share

    sections = []
    for dimension in DIMENSION_ORDER:
        current = [r for r in rows if r.period == period and r.dimension == dimension]
        if not current:
            continue
        total = totals.get((period, dimension), 0)
        buckets = []
        for row in current[:limit]:
            shares = history[(dimension, row.bucket)]
            share = shares.get(period, 0.0)
            before = shares.get(previous) if previous else None
            buckets.append(Bucket(
                label=row.bucket,
                count=row.count,
                share=share,
                delta=None if before is None else share - before,
                # Oldest first for reading left to right; ``window`` is newest first.
                spark=_spark_points([shares.get(p, 0.0) for p in reversed(window)]),
            ))
        sections.append({
            "dimension": dimension,
            "has_deltas": any(b.has_delta for b in buckets),
            "label": DIMENSION_LABELS.get(dimension, dimension),
            "buckets": buckets,
            "total": total,
            "truncated": max(0, len(current) - limit),
            "trendable": dimension in TREND_DIMENSIONS,
        })
    return sections


# One and a quarter times the 16-unit end-label font set in lumina-charts.css, which is
# a line box with a little air. Both are viewBox units and both are fixed, which is what
# makes this correct at every width: the collision question is gap against font size in
# viewBox units, and the render scale multiplies both and cancels. If the font there
# changes, change this with it.
_LABEL_GAP = 20.0


def _spread_labels(series: list[Series]) -> None:
    """Nudge end labels apart where two series finish at nearly the same share.

    Two lines ending three quarters of a point apart put their labels on top of each
    other and neither is readable, which is how a directly labelled chart loses the
    labels that were the reason it did not depend on colour. Only the label moves; the
    line still ends where the data says.
    """
    ordered = sorted(series, key=lambda s: s.label_y)
    for above, below in zip(ordered, ordered[1:], strict=False):
        overlap = _LABEL_GAP - (below.label_y - above.label_y)
        if overlap > 0:
            below.label_y += overlap
    # Anything pushed past the axis comes back inside, walking upwards this time.
    floor = VIEW_H - PAD_B
    for one in reversed(ordered):
        if one.label_y > floor:
            one.label_y = floor
            floor -= _LABEL_GAP


def trend(
    dimension: str,
    *,
    tier: str = SurveyStat.TIER_VERIFIED,
    periods: list[str] | None = None,
    limit: int = _SPARK_PERIODS,
    segment: str = "",
) -> Trend | None:
    """Share over time for one dimension: the top few buckets, and everything else.

    Ranked by the most recent period rather than by total, so the chart names what the
    fleet looks like now. Returns None when there is only one period, where a line chart
    would be a single dot claiming to show a direction.
    """
    known = periods if periods is not None else available_periods(segment)["month"]
    window = list(reversed(known[:limit]))    # oldest first
    if len(window) < 2:
        return None

    rows = list(
        SurveyStat.objects.filter(
            segment=segment, period__in=window, dimension=dimension, tier_scope=tier
        ).order_by("-count", "bucket")   # deterministic ties; see distribution()
    )
    if not rows:
        return None
    totals = _totals(rows)

    latest = window[-1]
    ranked = [r.bucket for r in rows if r.period == latest][:TREND_SERIES]
    if not ranked:
        ranked = [r.bucket for r in rows][:TREND_SERIES]

    shares: dict[str, dict[str, float]] = {}
    for row in rows:
        share = _share(row.count, totals.get((row.period, row.dimension), 0))
        name = row.bucket if row.bucket in ranked else OTHER_LABEL
        by_period = shares.setdefault(name, {})
        by_period[row.period] = by_period.get(row.period, 0.0) + share

    names = [n for n in ranked if n in shares]
    if OTHER_LABEL in shares:
        names.append(OTHER_LABEL)

    peak = max((v for by_period in shares.values() for v in by_period.values()), default=0.0)
    # Rounded up to a tidy gridline so the axis reads in tens, and never zero-height.
    y_max = min(100.0, max(10.0, 10.0 * ((peak // 10) + 1)))

    def x_of(index: int) -> float:
        step = _plot_w() / (len(window) - 1)
        return PAD_L + index * step

    def y_of(value: float) -> float:
        return PAD_T + _plot_h() * (1 - value / y_max)

    series = []
    for slot, name in enumerate(names):
        by_period = shares[name]
        values = [by_period.get(p, 0.0) for p in window]
        series.append(Series(
            label=name,
            # The remainder always wears the neutral slot, whatever its rank.
            slot=TREND_SERIES if name == OTHER_LABEL else slot,
            points=" ".join(
                f"{x_of(i):.1f},{y_of(v):.1f}" for i, v in enumerate(values)
            ),
            last=values[-1],
            label_y=y_of(values[-1]),
        ))

    _spread_labels(series)

    grid = [
        {"value": value, "y": y_of(value)}
        for value in (0, y_max / 2, y_max)
    ]
    # Only the ends and the middle: twelve month labels on a 720-unit axis collide.
    tick_indexes = sorted({0, len(window) // 2, len(window) - 1})
    x_ticks = [
        {"label": window[i], "x": x_of(i),
         "anchor": "start" if i == 0 else ("end" if i == len(window) - 1 else "middle")}
        for i in tick_indexes
    ]
    # Newest first, so the row a reader wants is the first one they see.
    table = [
        {"period": period,
         "cells": [shares[name].get(period, 0.0) for name in names]}
        for period in reversed(window)
    ]
    return Trend(
        dimension=dimension,
        label=DIMENSION_LABELS.get(dimension, dimension),
        periods=window,
        series=series,
        y_max=y_max,
        grid=grid,
        x_ticks=x_ticks,
        table=table,
    )


def machine_count(period: str, *, tier: str = SurveyStat.TIER_VERIFIED,
                  segment: str = "") -> int:
    """How many machines the period counted, from the rollup's own count row.

    Deliberately not the total of any one dimension: those count only the machines that
    reported that facet. Zero for a period rolled up before this row existed, which is
    what a rollup re-run fixes.
    """
    row = (
        SurveyStat.objects.filter(
            segment=segment, period=period, tier_scope=tier,
            dimension=SurveyStat.MACHINES_DIMENSION,
            bucket=SurveyStat.MACHINES_BUCKET,
        ).values_list("count", flat=True).first()
    )
    return row or 0


def page_context(params) -> dict:
    """Everything the statistics page shows of the survey, from its query string.

    Lives here rather than in the view because the survey app owns reading its own
    rollup, and because the page that renders it belongs to ``results``: without this
    the granularity and period rules would be written out a second time in another app
    and the two would drift.

    ``params`` is a request ``GET`` mapping, or any dict. An unknown or disabled
    ``segment`` falls back to the whole fleet rather than erroring, so a stale link or a
    segment an admin has since turned off still lands on a working page.
    """
    offered = list(SurveySegment.objects.filter(enabled=True))
    wanted = params.get("segment") or ""
    active = next((s for s in offered if s.slug == wanted), None)
    segment = active.slug if active else ""

    periods = available_periods(segment)
    granularity = params.get("by")
    if granularity not in ("month", "year"):
        # Months where there are any, because the trends are the point of the page.
        granularity = "month" if periods["month"] else "year"
    choices = periods[granularity]

    period = params.get("period")
    if period not in choices:
        period = choices[0] if choices else None

    sections, trends = [], []
    if period:
        sections = distribution(period, periods=choices, segment=segment)
        # Trends always read the monthly series, whatever granularity is selected: a
        # yearly line of two points is not a trend. The selected period still decides
        # which buckets are named, through the distribution above.
        for dimension in TREND_DIMENSIONS:
            one = trend(dimension, periods=periods["month"] or choices,
                        segment=segment)
            if one is not None:
                trends.append(one)

    by_dimension = {section["dimension"]: section for section in sections}
    headline = [
        {
            "label": DIMENSION_LABELS[dimension],
            "top": by_dimension[dimension]["buckets"][0],
            "total": by_dimension[dimension]["total"],
        }
        for dimension in HEADLINE_DIMENSIONS
        if by_dimension.get(dimension) and by_dimension[dimension]["buckets"]
    ]

    return {
        "period": period,
        "periods": choices,
        "headline": headline,
        "granularity": granularity,
        "has_months": bool(periods["month"]),
        "has_years": bool(periods["year"]),
        "sections": sections,
        "trends": trends,
        "machine_total": machine_count(period, segment=segment) if period else 0,
        "segments": offered,
        "segment": active,
    }
