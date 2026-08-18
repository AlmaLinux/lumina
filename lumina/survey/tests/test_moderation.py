"""Survey submissions are reviewable in their own queue - oversight, never a gate."""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.urls import reverse

from lumina.survey import services
from lumina.survey.models import SurveySubmission

pytestmark = pytest.mark.django_db
User = get_user_model()


def _reviewer():
    rev = User.objects.create_user(username="rev", password="x")
    rev.groups.add(Group.objects.get_or_create(name="reviewer")[0])
    return rev


def _sub(**kw) -> SurveySubmission:
    return SurveySubmission.objects.create(
        origin=SurveySubmission.ORIGIN_SURVEY,
        trust_tier=SurveySubmission.TIER_VERIFIED,
        **kw,
    )


def test_new_submission_shows_in_the_review_queue(client):
    _sub(cpu_model="AMD EPYC 9354", identity_hash="h")
    client.force_login(_reviewer())

    resp = client.get(reverse("review:queue"))
    assert resp.status_code == 200
    assert b"AMD EPYC 9354" in resp.content  # rendered in the survey pane


def test_dismiss_excludes_from_stats_and_records_the_reviewer(client):
    sub = _sub(identity_hash="h", cpu_vendor="AuthenticAMD")
    reviewer = _reviewer()
    client.force_login(reviewer)

    resp = client.post(reverse("review:survey_submission_dismiss", args=[sub.pk]))
    assert resp.status_code == 302
    sub.refresh_from_db()
    assert sub.review_state == SurveySubmission.REVIEW_DISMISSED
    assert sub.reviewed_by == reviewer
    assert sub not in SurveySubmission.objects.countable()


def test_accept_keeps_it_counting_but_clears_the_queue(client):
    sub = _sub(identity_hash="h", cpu_vendor="AuthenticAMD")
    client.force_login(_reviewer())

    client.post(reverse("review:survey_submission_accept", args=[sub.pk]))
    sub.refresh_from_db()
    assert sub.review_state == SurveySubmission.REVIEW_ACCEPTED
    assert sub in SurveySubmission.objects.countable()          # still counts
    assert sub not in SurveySubmission.objects.pending_review()  # off the queue


def test_a_new_submission_already_counts(client):
    # No submitter interaction, no reviewer action needed: it counts immediately.
    sub = _sub(identity_hash="h")
    assert sub.review_state == SurveySubmission.REVIEW_NEW
    assert sub in SurveySubmission.objects.countable()


def test_moderation_respects_the_append_only_guard():
    sub = _sub(identity_hash="h")
    services.moderate_submission(sub, by=None, dismiss=True)  # writes only operational cols
    sub.refresh_from_db()
    assert sub.review_state == SurveySubmission.REVIEW_DISMISSED


def test_detail_page_shows_facets_identity_and_raw_inventory(client):
    sub = _sub(
        identity_hash="dup", identity_source="smbios_uuid",
        cpu_model="AMD EPYC 9354", board_model="H13SSL-N", system_serial="SER-12345",
        inventory={"summary": {"system": {"product": "AS-2015HS-TNR"}}},
    )
    _sub(identity_hash="dup")  # a second report from the same machine
    client.force_login(_reviewer())

    body = client.get(
        reverse("review:survey_submission_detail", args=[sub.pk])
    ).content.decode()

    assert "AMD EPYC 9354" in body        # the extracted facet
    assert "AS-2015HS-TNR" in body        # the verbatim payload is inspectable
    assert "SER-12345" in body            # access-controlled identity, for a reviewer
    assert "other submission" in body     # the duplicate signal


def test_detail_page_is_reviewer_only(client):
    sub = _sub(identity_hash="h")
    client.force_login(User.objects.create_user(username="nobody", password="x"))
    resp = client.get(reverse("review:survey_submission_detail", args=[sub.pk]))
    assert resp.status_code == 403


def test_queue_row_links_to_the_detail(client):
    sub = _sub(identity_hash="h", cpu_model="AMD EPYC 9354")
    client.force_login(_reviewer())
    body = client.get(reverse("review:queue")).content.decode()
    assert reverse("review:survey_submission_detail", args=[sub.pk]) in body


def test_a_cert_run_fork_never_queues_for_survey_moderation(client):
    # A validate/benchmark run is reviewable in the runs queue. Its survey fork must not
    # ask a reviewer to moderate the same machine a second time - it only ever counts.
    fork = SurveySubmission.objects.create(
        origin=SurveySubmission.ORIGIN_CERT_RUN,
        trust_tier=SurveySubmission.TIER_VERIFIED,
        identity_hash="h", cpu_model="AMD EPYC 9354",
    )
    assert fork.review_state == SurveySubmission.REVIEW_NEW
    assert fork in SurveySubmission.objects.countable()
    assert fork not in SurveySubmission.objects.pending_review()

    client.force_login(_reviewer())
    resp = client.get(reverse("review:queue"))

    # Off the queue, but still visible and still actionable: a reviewer who spots a
    # bogus fork can dismiss it where they found it.
    assert fork not in resp.context["survey_submissions"]
    assert fork in resp.context["survey_recent"]
    assert fork.is_unreviewed


def test_the_survey_tab_is_there_even_with_nothing_waiting(client):
    """The census is a standing stream, so its tab does not come and go.

    It used to render only when the queue was non-empty, so it appeared when a
    submission landed and vanished again the moment it was accepted - which reads as a
    glitch, and left no way to tell whether submissions were arriving at all.
    """
    client.force_login(_reviewer())
    body = client.get(reverse("review:queue")).content.decode()

    assert "#tab-survey" in body
    assert "No survey submissions yet." in body


def test_an_accepted_submission_stays_listed_after_it_leaves_the_queue(client):
    sub = _sub(identity_hash="h", cpu_model="AMD EPYC 9354")
    reviewer = _reviewer()
    client.force_login(reviewer)

    client.post(reverse("review:survey_submission_accept", args=[sub.pk]))
    resp = client.get(reverse("review:queue"))

    assert list(resp.context["survey_submissions"]) == []   # nothing waiting
    assert sub in resp.context["survey_recent"]             # but not disappeared
    body = resp.content.decode()
    assert "AMD EPYC 9354" in body
    assert reviewer.get_username() in body                  # who acted on it
