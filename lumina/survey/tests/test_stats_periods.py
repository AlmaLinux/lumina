"""Monthly periods, and the shares and movement the statistics page reads off them.

``SurveyStat.period`` was always documented as holding "2026" or "2026-09", but the
rollup only ever emitted years, so the page had nothing to draw a trend from: one point
per year is not a direction. Both granularities are now real rows.

The rule that shapes everything here is that periods are never summed. Dedup happens
inside a period, so a machine reporting every month is one machine in each of those
months and one machine in the year. Adding twelve months together would count it twelve
times, which is why the page picks a granularity and stays inside it.
"""
from __future__ import annotations

import datetime as dt

import pytest

from lumina.survey import services, stats
from lumina.survey.models import SurveyStat, SurveySubmission

pytestmark = pytest.mark.django_db


def _sub(*, when, **kw):
    defaults = dict(
        origin=SurveySubmission.ORIGIN_SURVEY,
        trust_tier=SurveySubmission.TIER_VERIFIED,
    )
    defaults.update(kw)
    sub = SurveySubmission.objects.create(**defaults)
    # received_at is auto_now_add, so it is set after the fact to place the row in time.
    SurveySubmission.objects.filter(pk=sub.pk).update(received_at=when)
    sub.refresh_from_db()
    return sub


def _at(year, month, day=15):
    return dt.datetime(year, month, day, 12, 0, tzinfo=dt.UTC)


# --- the rollup ------------------------------------------------------------------

def test_the_rollup_writes_a_row_for_the_year_and_for_the_month():
    _sub(when=_at(2026, 9), identity_hash="a", cpu_vendor="GenuineIntel")

    services.rebuild_survey_stats()

    periods = set(SurveyStat.objects.values_list("period", flat=True))
    assert periods == {"2026", "2026-09"}


def test_a_machine_reporting_twice_in_a_month_is_one_machine():
    _sub(when=_at(2026, 9, 1), identity_hash="same", cpu_vendor="GenuineIntel")
    _sub(when=_at(2026, 9, 20), identity_hash="same", cpu_vendor="GenuineIntel")

    services.rebuild_survey_stats()

    monthly = SurveyStat.objects.get(period="2026-09", dimension="cpu_vendor",
                                     bucket="GenuineIntel", tier_scope="verified")
    assert monthly.count == 1


def test_the_same_machine_counts_once_in_each_month_it_reports():
    # And once in the year, which is exactly why the months are not summed into it.
    _sub(when=_at(2026, 8), identity_hash="same", cpu_vendor="GenuineIntel")
    _sub(when=_at(2026, 9), identity_hash="same", cpu_vendor="GenuineIntel")

    services.rebuild_survey_stats()

    def count(period):
        return SurveyStat.objects.get(period=period, dimension="cpu_vendor",
                                      bucket="GenuineIntel", tier_scope="verified").count

    assert count("2026-08") == 1
    assert count("2026-09") == 1
    assert count("2026") == 1, "the year is its own dedup, not the sum of its months"


def test_rebuilding_one_month_leaves_the_others_alone():
    _sub(when=_at(2026, 8), identity_hash="a", cpu_vendor="GenuineIntel")
    _sub(when=_at(2026, 9), identity_hash="b", cpu_vendor="GenuineIntel")
    services.rebuild_survey_stats()

    services.rebuild_survey_stats(period="2026-09")

    assert SurveyStat.objects.filter(period="2026-08").exists()


def test_the_rollup_is_idempotent_across_granularities():
    _sub(when=_at(2026, 9), identity_hash="a", cpu_vendor="GenuineIntel")
    services.rebuild_survey_stats()
    before = sorted(SurveyStat.objects.values_list("period", "dimension", "bucket", "count"))

    services.rebuild_survey_stats()

    assert sorted(
        SurveyStat.objects.values_list("period", "dimension", "bucket", "count")
    ) == before


# --- reading it ------------------------------------------------------------------

def test_periods_are_offered_split_by_granularity():
    _sub(when=_at(2026, 9), identity_hash="a", cpu_vendor="GenuineIntel")
    _sub(when=_at(2025, 3), identity_hash="b", cpu_vendor="GenuineIntel")
    services.rebuild_survey_stats()

    periods = stats.available_periods()

    assert periods["year"] == ["2026", "2025"]           # newest first
    assert periods["month"] == ["2026-09", "2025-03"]


def test_the_previous_period_skips_a_gap_rather_than_inventing_a_zero():
    # September against July, because August has no data at all. Comparing against an
    # empty August would report that every share collapsed to nothing.
    known = ["2026-09", "2026-07", "2026-06"]

    assert stats.previous_period("2026-09", known) == "2026-07"
    assert stats.previous_period("2026-06", known) is None


def test_shares_are_percentages_of_the_machines_in_that_dimension():
    for i, vendor in enumerate(["GenuineIntel"] * 3 + ["AuthenticAMD"]):
        _sub(when=_at(2026, 9), identity_hash=f"h{i}", cpu_vendor=vendor)
    services.rebuild_survey_stats()

    section = next(s for s in stats.distribution("2026-09")
                   if s["dimension"] == "cpu_vendor")

    assert section["total"] == 4
    assert [(b.label, round(b.share, 1)) for b in section["buckets"]] == [
        ("GenuineIntel", 75.0), ("AuthenticAMD", 25.0),
    ]


def test_movement_is_percentage_points_against_the_previous_period():
    _sub(when=_at(2026, 8), identity_hash="a", cpu_vendor="GenuineIntel")
    _sub(when=_at(2026, 8), identity_hash="b", cpu_vendor="AuthenticAMD")
    # September: Intel takes three of four, up from one of two.
    for i, vendor in enumerate(["GenuineIntel"] * 3 + ["AuthenticAMD"]):
        _sub(when=_at(2026, 9), identity_hash=f"s{i}", cpu_vendor=vendor)
    services.rebuild_survey_stats()

    section = next(s for s in stats.distribution("2026-09")
                   if s["dimension"] == "cpu_vendor")
    intel = next(b for b in section["buckets"] if b.label == "GenuineIntel")

    assert round(intel.delta, 1) == 25.0     # 75% now, 50% then
    assert intel.direction == "up"


def test_a_first_period_reports_no_movement_rather_than_no_change():
    _sub(when=_at(2026, 9), identity_hash="a", cpu_vendor="GenuineIntel")
    services.rebuild_survey_stats()

    section = next(s for s in stats.distribution("2026-09")
                   if s["dimension"] == "cpu_vendor")
    bucket = section["buckets"][0]

    assert bucket.has_delta is False
    assert bucket.direction == "none", "nothing to compare against is not the same as flat"
    assert section["has_deltas"] is False
