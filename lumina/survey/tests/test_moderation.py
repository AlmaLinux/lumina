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


def test_a_fork_offers_no_buttons_and_is_not_counted_as_waiting(client):
    """A cert-run fork is evidence attached to a run that is reviewable in its own queue.

    It used to carry Accept and Dismiss here, which asked a reviewer to judge the same
    machine twice from two pages with nothing tying the two decisions together. It is
    listed for visibility and decided with its run.
    """
    fork = SurveySubmission.objects.create(
        origin=SurveySubmission.ORIGIN_CERT_RUN,
        trust_tier=SurveySubmission.TIER_VERIFIED,
        identity_hash="h", cpu_model="AMD EPYC 9354",
    )
    _sub(identity_hash="standalone", cpu_model="Intel Xeon Gold 6430")

    client.force_login(_reviewer())
    resp = client.get(reverse("review:queue"))

    assert fork.can_moderate is False
    assert fork.follows_run is True
    assert fork not in resp.context["survey_submissions"], "not waiting on anybody here"
    assert len(resp.context["survey_submissions"]) == 1
    assert fork in resp.context["survey_recent"], "still listed"
    assert fork in SurveySubmission.objects.countable(), "and still counted in the census"


def test_moderating_a_fork_directly_is_refused(client):
    fork = SurveySubmission.objects.create(
        origin=SurveySubmission.ORIGIN_CERT_RUN,
        trust_tier=SurveySubmission.TIER_VERIFIED,
        identity_hash="h",
    )

    with pytest.raises(services.NotDirectlyModeratable):
        services.moderate_submission(fork, by=None, dismiss=True)

    fork.refresh_from_db()
    assert fork.review_state == SurveySubmission.REVIEW_NEW


def test_the_view_says_where_the_decision_belongs(client):
    fork = SurveySubmission.objects.create(
        origin=SurveySubmission.ORIGIN_CERT_RUN,
        trust_tier=SurveySubmission.TIER_VERIFIED,
        identity_hash="h",
    )
    client.force_login(_reviewer())

    resp = client.post(
        reverse("review:survey_submission_dismiss", args=[fork.pk]), follow=True
    )

    assert "decided with that run" in resp.content.decode()
    fork.refresh_from_db()
    assert fork.review_state == SurveySubmission.REVIEW_NEW

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


# --- moderating refreshes the published numbers ----------------------------------


def test_dismissing_takes_the_machine_out_of_the_published_stats_at_once():
    """Without this the page kept counting a machine a reviewer had just dismissed until
    the next rollup, which is up to an hour: a reviewer acts on something plainly wrong
    and the wrong number stays up."""
    from lumina.survey import stats

    sub = _sub(identity_hash="h", cpu_vendor="AuthenticAMD")
    services.rebuild_survey_stats()
    period = stats.available_periods()["month"][0]
    assert any(s["dimension"] == "cpu_vendor" for s in stats.distribution(period))

    services.moderate_submission(sub, by=None, dismiss=True)

    assert not any(s["dimension"] == "cpu_vendor" for s in stats.distribution(period))


def test_accepting_refreshes_the_period_even_though_the_count_is_unchanged():
    # An unreviewed submission already counts, so the number does not move. The rebuild
    # still happens, so accept and dismiss behave the same way from the reviewer's side.
    from lumina.survey import stats

    sub = _sub(identity_hash="h", cpu_vendor="AuthenticAMD")

    assert services.refresh_period_for(sub) is not None

    services.moderate_submission(sub, by=None, dismiss=False)
    period = stats.available_periods()["month"][0]
    section = next(s for s in stats.distribution(period) if s["dimension"] == "cpu_vendor")

    assert section["total"] == 1


def test_the_month_rebuilt_is_the_submissions_own_not_todays():
    import datetime as dt

    sub = _sub(identity_hash="h", cpu_vendor="AuthenticAMD")
    SurveySubmission.objects.filter(pk=sub.pk).update(
        received_at=dt.datetime(2024, 3, 9, 12, tzinfo=dt.UTC)
    )
    sub.refresh_from_db()

    assert services.refresh_period_for(sub) == "2024-03"


def test_a_rollup_failure_does_not_undo_the_decision(monkeypatch):
    sub = _sub(identity_hash="h", cpu_vendor="AuthenticAMD")

    def boom(*args, **kwargs):
        raise RuntimeError("rollup exploded")

    monkeypatch.setattr(services, "rebuild_survey_stats", boom)
    services.moderate_submission(sub, by=None, dismiss=True)
    sub.refresh_from_db()

    assert sub.review_state == SurveySubmission.REVIEW_DISMISSED
