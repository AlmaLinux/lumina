"""Rebuilding the published statistics from the review queue.

The statistics page reads the rollup and never the submissions, so nothing a reviewer does
reaches it until a rebuild runs. Timers cover steady state; this covers the two moments
when somebody is watching: just after a deploy that changes how a facet is derived, when
every historical row is stale, and while checking whether a machine that was just
submitted came through.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth.models import Group, User
from django.core.cache import cache
from django.urls import reverse

from lumina.survey.models import SurveyStat, SurveySubmission

pytestmark = pytest.mark.django_db


@pytest.fixture
def reviewer(client):
    user = User.objects.create_user("rollup-rev", password="pw")
    user.groups.add(Group.objects.get_or_create(name="reviewer")[0])
    client.force_login(user)
    return user


def _sub(*, when=None, **kw):
    defaults = dict(
        origin=SurveySubmission.ORIGIN_SURVEY,
        trust_tier=SurveySubmission.TIER_VERIFIED,
        identity_hash="h", cpu_vendor="GenuineIntel",
    )
    defaults.update(kw)
    sub = SurveySubmission.objects.create(**defaults)
    if when:
        SurveySubmission.objects.filter(pk=sub.pk).update(received_at=when)
    return sub


def test_the_button_publishes_a_submission_that_had_not_been_rolled_up(client, reviewer):
    _sub()
    assert not SurveyStat.objects.exists(), "nothing published until a rebuild runs"

    response = client.post(reverse("review:survey_rollup_now"), {"scope": "current"})

    assert response["Location"] == reverse("review:queue") + "?tab=survey"
    assert SurveyStat.objects.filter(dimension="cpu_vendor", bucket="Intel").exists()


def test_the_current_scope_leaves_other_periods_alone(client, reviewer):
    """The cheap button: the current month is the only period submissions still arrive in."""
    _sub(when=dt.datetime(2024, 5, 4, 12, tzinfo=dt.UTC), identity_hash="old")
    _sub(identity_hash="now")

    client.post(reverse("review:survey_rollup_now"), {"scope": "current"})

    assert not SurveyStat.objects.filter(period="2024-05").exists()
    assert SurveyStat.objects.exists()


def test_rebuilding_everything_reaches_history(client, reviewer):
    """The expensive button, and the reason it exists: a deploy that changes how a facet is
    derived leaves every historical row stale, and only a full rebuild corrects them."""
    _sub(when=dt.datetime(2024, 5, 4, 12, tzinfo=dt.UTC), identity_hash="old")

    client.post(reverse("review:survey_rollup_now"), {"scope": "all"})

    assert SurveyStat.objects.filter(period="2024-05").exists()
    assert SurveyStat.objects.filter(period="2024").exists()


def test_a_second_click_while_one_is_running_is_refused_not_queued(client, reviewer):
    # A rebuild deletes and recreates a period's rows, so two at once contend for them.
    _sub()
    cache.add("survey:rollup-running", True, 600)
    try:
        response = client.post(reverse("review:survey_rollup_now"), {"scope": "current"})
    finally:
        cache.delete("survey:rollup-running")

    assert response.status_code == 302
    assert not SurveyStat.objects.exists(), "the second click did not also rebuild"


def test_the_lock_is_released_so_the_next_click_works(client, reviewer):
    _sub()

    client.post(reverse("review:survey_rollup_now"), {"scope": "current"})
    SurveyStat.objects.all().delete()
    client.post(reverse("review:survey_rollup_now"), {"scope": "current"})

    assert SurveyStat.objects.exists()


def test_a_failed_rebuild_reports_itself_and_keeps_the_submissions(
    client, reviewer, monkeypatch
):
    _sub()
    from lumina.survey import services

    monkeypatch.setattr(
        services, "rebuild_survey_stats",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    response = client.post(reverse("review:survey_rollup_now"), {"scope": "current"})

    assert response.status_code == 302
    assert SurveySubmission.objects.count() == 1, "the raw layer is untouched"
    # And the lock is freed, or the button would be dead until it expired.
    assert cache.get("survey:rollup-running") is None


def test_only_a_reviewer_can_trigger_it(client):
    client.force_login(User.objects.create_user("nobody", password="pw"))

    response = client.post(reverse("review:survey_rollup_now"), {"scope": "all"})

    assert response.status_code in (302, 403)
    assert not SurveyStat.objects.exists()


def test_it_refuses_a_get(client, reviewer):
    assert client.get(reverse("review:survey_rollup_now")).status_code == 405


def test_the_buttons_are_on_the_survey_pane(client, reviewer):
    body = client.get(reverse("review:queue")).content.decode()

    assert reverse("review:survey_rollup_now") in body
    assert "Rebuild this month" in body
    assert "Rebuild every period" in body
