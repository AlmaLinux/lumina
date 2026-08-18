"""The public statistics page: period controls, trend charts, and the ranked detail.

The page lives at ``results:stats``, not in this app: there is one public statistics
page, and it carries the survey census alongside the certification totals with the
difference between the two populations stated on it. The survey half is built here, so
its tests are here.

Modelled on the Steam hardware survey: what the fleet looks like in one period, and
which way each part of it is moving. The chart geometry is computed in
``lumina.survey.stats`` rather than in the template precisely so it can be asserted on
here, because a template that does its own arithmetic renders nonsense silently when a
series is empty or flat.

The series cap is a measured constraint, not a layout preference. The AlmaLinux brand
hues support three chromatic series alongside the neutral remainder: brand red and brand
yellow are indistinguishable to a red-green colour-blind reader, and no fourth step
clears the separation floor against the other three. So a fourth bucket folds into
"Other" and dimensions with many buckets get a sparkline per row instead of more lines.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.urls import reverse

from lumina.survey import services, stats
from lumina.survey.models import SurveySubmission

pytestmark = pytest.mark.django_db


def _sub(*, when, **kw):
    defaults = dict(
        origin=SurveySubmission.ORIGIN_SURVEY,
        trust_tier=SurveySubmission.TIER_VERIFIED,
    )
    defaults.update(kw)
    sub = SurveySubmission.objects.create(**defaults)
    SurveySubmission.objects.filter(pk=sub.pk).update(received_at=when)
    return sub


def _at(year, month, day=15):
    return dt.datetime(year, month, day, 12, 0, tzinfo=dt.UTC)


def _two_months_of_gpus():
    """August: NVIDIA leads. September: five vendors with no tie in the top three, so
    the ranking is unambiguous and ASPEED and Matrox fall into the remainder."""
    # The other facets are set too, so the at-a-glance band has all four of its tiles.
    # They do not vary, which keeps every GPU assertion below reading off GPU alone.
    common = dict(
        arch="x86_64", cpu_vendor="GenuineIntel",
        memory_bytes=128 * 1024 ** 3, os_major=9, os_minor=6,
    )
    for i, vendor in enumerate(["NVIDIA"] * 2 + ["AMD"]):
        _sub(when=_at(2026, 8), identity_hash=f"a{i}", gpu_vendor=vendor, **common)
    september = (["NVIDIA"] * 4 + ["AMD"] * 3 + ["Intel"] * 2 + ["ASPEED"] + ["Matrox"])
    for i, vendor in enumerate(september):
        _sub(when=_at(2026, 9), identity_hash=f"s{i}", gpu_vendor=vendor, **common)
    services.rebuild_survey_stats()


# --- trend charts ----------------------------------------------------------------

def test_a_trend_names_the_top_three_and_folds_the_rest():
    _two_months_of_gpus()

    trend = stats.trend("gpu_vendor")

    labels = [s.label for s in trend.series]
    assert labels[:3] == ["NVIDIA", "AMD", "Intel"]   # ranked by the latest period
    assert labels[-1] == "Other"                       # ASPEED and Matrox, together
    assert len(trend.series) == 4, "three chromatic series plus the remainder, no more"


def test_the_remainder_always_wears_the_neutral_slot():
    _two_months_of_gpus()

    other = next(s for s in stats.trend("gpu_vendor").series if s.label == "Other")

    assert other.slot == stats.TREND_SERIES, "Other is never given a brand hue"


def test_the_folded_remainder_sums_the_buckets_it_replaces():
    _two_months_of_gpus()

    trend = stats.trend("gpu_vendor")
    other = next(s for s in trend.series if s.label == "Other")

    # ASPEED and Matrox are one machine each of eleven in September.
    assert round(other.last, 1) == round(200.0 / 11, 1)


def test_one_period_is_not_a_trend():
    _sub(when=_at(2026, 9), identity_hash="a", gpu_vendor="NVIDIA")
    services.rebuild_survey_stats()

    assert stats.trend("gpu_vendor") is None, "a single point cannot show a direction"


def test_shares_in_a_period_add_up_to_everything():
    _two_months_of_gpus()

    trend = stats.trend("gpu_vendor")

    # table[0] is the newest period; the named series plus the remainder are everything.
    assert round(sum(trend.table[0]["cells"]), 0) == 100.0


# --- chart geometry --------------------------------------------------------------

def test_every_point_lands_inside_the_plot_area():
    _two_months_of_gpus()

    trend = stats.trend("gpu_vendor")

    for series in trend.series:
        for pair in series.points.split():
            x, y = (float(v) for v in pair.split(","))
            assert stats.PAD_L <= x <= stats.VIEW_W - stats.PAD_R
            assert stats.PAD_T <= y <= stats.VIEW_H - stats.PAD_B


def test_the_y_axis_is_a_tidy_round_number_above_the_peak():
    _two_months_of_gpus()

    trend = stats.trend("gpu_vendor")
    peak = max(c for row in trend.table for c in row["cells"])

    assert trend.y_max >= peak
    assert trend.y_max % 10 == 0
    assert trend.y_max <= 100.0


def test_a_flat_series_still_draws_a_line():
    # A bucket at a steady share must not divide by a zero range and vanish.
    for month in (8, 9):
        for i in range(2):
            _sub(when=_at(2026, month), identity_hash=f"{month}-{i}", gpu_vendor="NVIDIA")
    services.rebuild_survey_stats()

    trend = stats.trend("gpu_vendor")
    nvidia = trend.series[0]

    assert len(nvidia.points.split()) == 2
    assert "nan" not in nvidia.points.lower()


def test_no_sparkline_until_there_is_history_to_draw():
    """One period is not a trend. It used to render as a flat two-point line, so every
    row carried a horizontal dash that read as "steady" when nothing had been measured
    twice."""
    _sub(when=_at(2026, 9), identity_hash="a", gpu_vendor="NVIDIA")
    services.rebuild_survey_stats()

    bucket = next(
        b for s in stats.distribution("2026-09") if s["dimension"] == "gpu_vendor"
        for b in s["buckets"]
    )

    assert bucket.spark == ""


def test_a_sparkline_appears_once_a_second_period_exists():
    _two_months_of_gpus()

    bucket = next(
        b for s in stats.distribution("2026-09") if s["dimension"] == "gpu_vendor"
        for b in s["buckets"] if b.label == "NVIDIA"
    )

    assert len(bucket.spark.split()) == 2, "one point per period in the window"


# --- the page --------------------------------------------------------------------

def test_the_page_renders_charts_and_the_period_controls(client):
    _two_months_of_gpus()

    body = client.get(reverse("results:stats")).content.decode()

    assert "Hardware statistics" in body
    assert "hardware survey" in body
    assert 'name="by"' in body and 'name="period"' in body   # month/year, and which
    assert "chart-trend" in body                              # the over-time SVG
    assert "chart-spark" in body                              # per-row history
    assert "NVIDIA" in body


def test_the_page_defaults_to_the_newest_month(client):
    _two_months_of_gpus()

    response = client.get(reverse("results:stats"))

    assert response.context["granularity"] == "month"
    assert response.context["period"] == "2026-09"


def test_a_year_can_be_asked_for(client):
    _two_months_of_gpus()

    response = client.get(reverse("results:stats"), {"by": "year", "period": "2026"})

    assert response.context["period"] == "2026"
    assert response.context["granularity"] == "year"


def test_a_junk_period_falls_back_rather_than_erroring(client):
    _two_months_of_gpus()

    response = client.get(reverse("results:stats"),
                          {"by": "nonsense", "period": "'; drop table --"})

    assert response.status_code == 200
    assert response.context["period"] == "2026-09"


def test_an_empty_survey_says_so(client):
    body = client.get(reverse("results:stats")).content.decode()

    assert "Nothing to show yet" in body


def test_the_numbers_are_available_as_a_table_not_only_a_picture(client):
    _two_months_of_gpus()

    body = client.get(reverse("results:stats")).content.decode()

    # Identity is never colour-alone: a legend, a direct label per line, and the figures.
    assert "chart-legend" in body
    assert "Show these numbers as a table" in body


def test_a_tie_breaks_on_the_name_so_the_chart_does_not_reshuffle():
    """Server GPUs tie at a count of one constantly. Without a deterministic tiebreak the
    series a trend names can change between page loads for no reason a reader can see."""
    for vendor in ("Matrox", "ASPEED", "Intel"):
        for month in (8, 9):
            _sub(when=_at(2026, month), identity_hash=f"{vendor}-{month}", gpu_vendor=vendor)
    services.rebuild_survey_stats()

    once = [s.label for s in stats.trend("gpu_vendor").series]
    again = [s.label for s in stats.trend("gpu_vendor").series]

    assert once == again == ["ASPEED", "Intel", "Matrox"]


def test_end_labels_never_sit_on_top_of_each_other():
    """Two series finishing at a similar share must not stack their labels.

    Direct labels are how the chart avoids resting identity on colour alone, so a pair
    that overprints loses exactly the thing they were there for. The nudge is computed
    on the server against a fixed font size: whether two labels collide is gap against
    font size in viewBox units, and the render scale multiplies both and cancels, which
    is why the font in lumina-charts.css does not vary between breakpoints.
    """
    # Three vendors within a point of each other, plus a clear leader.
    for month in (8, 9):
        picks = ["NVIDIA"] * 10 + ["AMD"] * 3 + ["Intel"] * 3 + ["ASPEED"] * 3
        for i, vendor in enumerate(picks):
            _sub(when=_at(2026, month), identity_hash=f"{month}-{i}", gpu_vendor=vendor)
    services.rebuild_survey_stats()

    trend = stats.trend("gpu_vendor")
    ys = sorted(s.label_y for s in trend.series)

    assert len(ys) == 4
    gaps = [below - above for above, below in zip(ys, ys[1:], strict=False)]
    assert min(gaps) >= stats._LABEL_GAP - 0.01, f"labels {ys} would overprint"


def test_a_nudged_label_stays_inside_the_chart():
    for month in (8, 9):
        for i, vendor in enumerate(["A"] * 4 + ["B"] * 4 + ["C"] * 4 + ["D"] * 4):
            _sub(when=_at(2026, month), identity_hash=f"{month}-{i}", gpu_vendor=vendor)
    services.rebuild_survey_stats()

    trend = stats.trend("gpu_vendor")

    for series in trend.series:
        assert stats.PAD_T <= series.label_y <= stats.VIEW_H - stats.PAD_B


def test_the_old_survey_url_still_lands_on_the_page(client):
    """The survey statistics used to have a page of their own. Anything linking to it
    should arrive at the merged page rather than a 404."""
    response = client.get("/survey/")

    assert response.status_code == 302
    assert response["Location"] == reverse("results:stats")


def test_an_empty_page_draws_no_charts_or_band(client):
    """A fresh install has nothing rolled up yet, and every part of the page reads off
    that rollup, so none of it may render half-built against an empty context."""
    response = client.get(reverse("results:stats"))
    body = response.content.decode()

    assert response.status_code == 200
    assert response.context["headline"] == []
    assert "chart-trend" not in body
    assert "stat-band" not in body


def test_the_survey_half_states_its_own_counting_rule(client):
    _two_months_of_gpus()

    response = client.get(reverse("results:stats"))

    assert response.context["sections"]
    assert "never summed" in response.content.decode()


# --- the at-a-glance band --------------------------------------------------------

def test_the_band_leads_with_the_machine_count_and_the_top_answers(client):
    _two_months_of_gpus()

    response = client.get(reverse("results:stats"))
    labels = [card["label"] for card in response.context["headline"]]

    assert response.context["machine_total"] == 11
    # The four dimensions most people came for, in reading order.
    assert labels == ["CPU vendor", "GPU vendor", "Memory", "AlmaLinux version"]
    assert "stat-band" in response.content.decode()


def test_the_band_names_the_leader_and_its_share():
    _two_months_of_gpus()

    context = stats.page_context({})
    gpu = next(c for c in context["headline"] if c["label"] == "GPU vendor")

    assert gpu["top"].label == "NVIDIA"
    assert round(gpu["top"].share, 1) == round(400.0 / 11, 1)   # four of eleven


def test_the_band_skips_a_dimension_with_nothing_in_it():
    # Memory is not reported by these submissions, so it has no tile rather than an
    # empty one claiming a leader of "".
    for i in range(3):
        _sub(when=_at(2026, 9), identity_hash=f"h{i}", cpu_vendor="GenuineIntel")
    services.rebuild_survey_stats()

    labels = [c["label"] for c in stats.page_context({})["headline"]]

    assert "CPU vendor" in labels
    assert "Memory" not in labels
