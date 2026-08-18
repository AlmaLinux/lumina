"""A decision returns the reviewer to the tab they made it on.

Every decision view used to redirect to a bare ``review:queue``, which reopens whichever
tab the markup marks active: a reviewer working down the survey pane was thrown back to
hardware submissions after each accept, and had to find their place again. The queue is
the page a reviewer keeps open all day, so this is the difference between working through
a queue and fighting it.

The tab travels as ``?tab=<slug>`` because that is what ``static/js/tab-url.js`` already
reads on load, so it survives the redirect with no new mechanism.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import Group, User
from django.urls import reverse

from lumina.review.views import QUEUE_TABS, queue_redirect
from lumina.survey.models import SurveySubmission

pytestmark = pytest.mark.django_db


@pytest.fixture
def reviewer(client):
    user = User.objects.create_user("tab-rev", password="pw")
    user.groups.add(Group.objects.get_or_create(name="reviewer")[0])
    client.force_login(user)
    return user


def _submission():
    return SurveySubmission.objects.create(
        origin=SurveySubmission.ORIGIN_SURVEY,
        trust_tier=SurveySubmission.TIER_VERIFIED,
        identity_hash="h", cpu_vendor="GenuineIntel",
    )


def test_accepting_a_survey_submission_stays_on_the_survey_tab(client, reviewer):
    sub = _submission()

    response = client.post(reverse("review:survey_submission_accept", args=[sub.pk]))

    assert response.status_code == 302
    assert response["Location"] == reverse("review:queue") + "?tab=survey"


def test_dismissing_stays_there_too(client, reviewer):
    sub = _submission()

    response = client.post(reverse("review:survey_submission_dismiss", args=[sub.pk]))

    assert response["Location"] == reverse("review:queue") + "?tab=survey"


def test_every_tab_slug_names_a_pane_that_exists():
    """The slug is the pane id with "tab-" dropped, which is the contract tab-url.js
    implements. A redirect naming a slug with no pane silently opens the default instead,
    which looks exactly like the bug this fixed.

    Read from the template rather than a rendered page: two panes (quarantined runs,
    survey tokens) only render when they have something in them, so a rendered empty
    queue would not prove a slug wrong.
    """
    import pathlib

    from django.conf import settings

    markup = ""
    for root in settings.TEMPLATES[0]["DIRS"]:
        candidate = pathlib.Path(root) / "review" / "queue.html"
        if candidate.exists():
            markup = candidate.read_text()
            break
    assert markup, "review/queue.html not found"

    for slug in QUEUE_TABS:
        assert f'id="tab-{slug}"' in markup, slug


def test_an_unknown_tab_is_dropped_rather_than_sent_on():
    # Defence in depth: the slug reaches a URL, so it is validated against the panes that
    # exist rather than interpolated on trust.
    assert queue_redirect("nonsense")["Location"] == reverse("review:queue")
    assert queue_redirect("")["Location"] == reverse("review:queue")


def test_every_named_tab_is_actually_reachable():
    assert "survey" in QUEUE_TABS
    assert queue_redirect("survey")["Location"].endswith("?tab=survey")
