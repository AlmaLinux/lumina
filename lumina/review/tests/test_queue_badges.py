"""Queue depth, on every tab and on the sidebar link.

The badges are a routing device, not decoration: a reviewer opening lumina should be able
to see whether anything is waiting without visiting the queue, and once there, which tab
needs them. So a non-zero count is red and a zero is grey, everywhere, and the sidebar
carries the total across every queue.

The risk this file guards is drift. The total is summed from a registry of queues in
``review.queue_counts`` while the tabs count their own rows in the view, so a queue added
to the page and forgotten here would be invisible in the sidebar. The last test pins the
two lists against each other.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import Group, User
from django.urls import reverse

from lumina.review import queue_counts
from lumina.survey.models import SurveySubmission

pytestmark = pytest.mark.django_db


@pytest.fixture
def reviewer(client):
    user = User.objects.create_user("badge-rev", password="pw")
    user.groups.add(Group.objects.get_or_create(name="reviewer")[0])
    client.force_login(user)
    return user


def _survey_pending(n=1):
    for i in range(n):
        SurveySubmission.objects.create(
            origin=SurveySubmission.ORIGIN_SURVEY,
            trust_tier=SurveySubmission.TIER_VERIFIED,
            identity_hash=f"h{i}", cpu_vendor="GenuineIntel",
        )


# --- the tab badges ---------------------------------------------------------------

def test_an_empty_queue_shows_a_grey_zero_rather_than_nothing(client, reviewer):
    body = client.get(reverse("review:queue")).content.decode()

    # "Nothing waiting" is a statement the page makes, not an absence to infer.
    assert 'class="badge text-bg-secondary ms-1">0</span>' in body


def test_a_waiting_queue_turns_red(client, reviewer):
    _survey_pending(3)

    body = client.get(reverse("review:queue")).content.decode()

    assert 'class="badge text-bg-danger ms-1">3</span>' in body


# --- the sidebar total ------------------------------------------------------------

def test_the_sidebar_totals_every_queue(client, reviewer):
    _survey_pending(2)

    body = client.get(reverse("review:queue")).content.decode()

    assert queue_counts.total(use_cache=False) == 2
    assert 'class="badge ms-auto text-bg-danger">2</span>' in body


def test_the_sidebar_shows_a_grey_zero_when_nothing_is_waiting(client, reviewer):
    body = client.get(reverse("review:queue")).content.decode()

    assert queue_counts.total(use_cache=False) == 0
    assert 'class="badge ms-auto text-bg-secondary">0</span>' in body


def test_the_total_is_not_shown_to_somebody_who_cannot_review(client):
    client.force_login(User.objects.create_user("nobody", password="pw"))

    body = client.get(reverse("accounts:dashboard")).content.decode()

    assert "Review queue" not in body


def test_the_total_is_cached_briefly_so_every_page_is_not_eleven_counts(client, reviewer):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    queue_counts.total()                      # warm it

    with CaptureQueriesContext(connection) as ctx:
        queue_counts.total()

    assert len(ctx.captured_queries) == 0


# --- the drift guard --------------------------------------------------------------

def test_every_queue_on_the_page_is_counted_in_the_total(client, reviewer):
    """A queue shown on the page but missing from the registry would be waiting work the
    sidebar never mentions, which is worse than no badge at all: it would read as "all
    clear" while something sat unreviewed.

    Keyed by tab, so adding a tab means adding a counter. If this fails, add the queue to
    ``review.queue_counts._counters`` rather than deleting the expectation.
    """
    expected = {
        "submissions", "vendors", "listing_edits", "software",
        "validation_runs", "benchmark_runs", "quarantined_runs",
        "survey_tokens", "survey",
    }

    assert set(queue_counts.queue_counts()) == expected

    # And every one of them is a tab on the page.
    body = client.get(reverse("review:queue")).content.decode()
    for target in ("#tab-submissions", "#tab-vendors", "#tab-edits", "#tab-software",
                   "#tab-validation-runs", "#tab-benchmark-runs", "#tab-survey"):
        assert target in body, target


def test_the_counters_agree_with_what_the_page_counted(client, reviewer):
    """The page counts rows it already loaded; the registry runs COUNT queries. Two paths
    to the same number, so they are checked against each other on real data."""
    _survey_pending(4)

    response = client.get(reverse("review:queue"))
    counts = queue_counts.queue_counts()

    assert counts["survey"] == len(response.context["survey_submissions"])
    assert counts["validation_runs"] == len(response.context["validation_runs"])
    assert counts["benchmark_runs"] == len(response.context["benchmark_runs"])
    assert counts["quarantined_runs"] == len(response.context["quarantined_runs"])
    assert counts["submissions"] == len(response.context["submissions"])
    assert counts["vendors"] == response.context["vendor_queue_count"]
    assert counts["software"] == response.context["software_queue_count"]
    assert counts["listing_edits"] == len(response.context["listing_edits"])


def test_the_survey_badge_equals_the_rows_that_offer_a_decision(client, reviewer):
    """The bug this pins: the tab read "1" beside three rows with working Accept and
    Dismiss buttons, because the count and the buttons were computed from different rules.

    Asserted against the rendered page rather than the queryset, because the queryset was
    self-consistent the whole time. What was wrong was the page.
    """
    import re

    from lumina.survey.models import SurveySubmission

    _survey_pending(2)
    # Cert-run forks are listed but carry no buttons: they are decided with their run, so
    # they must not be counted as waiting here either.
    for i in range(3):
        SurveySubmission.objects.create(
            origin=SurveySubmission.ORIGIN_CERT_RUN,
            trust_tier=SurveySubmission.TIER_VERIFIED,
            identity_hash=f"fork{i}", cpu_vendor="GenuineIntel",
        )

    response = client.get(reverse("review:queue"))
    body = response.content.decode()

    # One accept form per row a reviewer can act on.
    actionable = len(set(re.findall(r"/review/survey/(\d+)/accept/", body)))
    badge = len(response.context["survey_submissions"])

    assert actionable == 2
    assert badge == actionable, (
        f"the tab says {badge} while {actionable} rows offer a decision"
    )
    assert f'class="badge text-bg-danger ms-1">{actionable}</span>' in body
