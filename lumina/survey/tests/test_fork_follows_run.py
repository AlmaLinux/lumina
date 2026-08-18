"""A cert-run fork is decided by the run it came from, not on its own.

A validate or benchmark run forks a census record from its inventory. That record used to
carry Accept and Dismiss in the survey queue, which asked a reviewer to judge the same
machine twice, from two pages, with nothing tying the two decisions together: a run could
be rejected while its survey record sat accepted, and the published census would count a
machine whose evidence the platform had refused.

Now approving a run marks its fork reviewed. Rejecting one deliberately does **not** touch
it: the census asks what hardware exists and runs AlmaLinux, not what passed certification,
so a machine whose run was rejected for failing a test is still a real machine and still
counts. Dropping it would quietly turn the published figures into a survey of successful
certifications, which is a different and much less useful question.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import Group, User

from lumina.results import ingest
from lumina.results import services as run_services
from lumina.results.models import TestRun
from lumina.results.tests import factories as f
from lumina.survey.models import SurveySubmission

pytestmark = pytest.mark.django_db


@pytest.fixture
def submitter():
    return User.objects.create_user("fork-sub", email="fs@example.com")


@pytest.fixture
def reviewer():
    user = User.objects.create_user("fork-rev", email="fr@example.com")
    user.groups.add(Group.objects.get_or_create(name="reviewer")[0])
    return user


def _benchmark_run(submitter):
    """A benchmark run, which lands straight in the queue rather than as a draft."""
    report = f.make_report(run_types=["benchmark"], results=[])
    return ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(f.build_bundle(report)),
        source="api",
    )


def _fork_of(run) -> SurveySubmission:
    return SurveySubmission.objects.get(source_run=run)


def test_a_fork_records_the_run_it_came_from(submitter):
    run = _benchmark_run(submitter)

    fork = _fork_of(run)

    assert fork.origin == SurveySubmission.ORIGIN_CERT_RUN
    assert fork.follows_run is True
    assert fork.can_moderate is False


def test_approving_the_run_accepts_its_fork(submitter, reviewer):
    run = _benchmark_run(submitter)

    run_services.approve_run(run, by=reviewer)

    fork = _fork_of(run)
    assert fork.review_state == SurveySubmission.REVIEW_ACCEPTED
    assert fork.reviewed_by == reviewer
    assert fork in SurveySubmission.objects.countable()


def test_rejecting_the_run_leaves_its_machine_in_the_census(submitter, reviewer):
    """The census counts hardware that exists, not hardware that passed.

    A run rejected for failing a test, or for wrong listing details, still came from a real
    machine with a real inventory. Removing it would make the published figures a survey of
    successful certifications instead.
    """
    run = _benchmark_run(submitter)

    run_services.reject_run(run, by=reviewer, reason="not plausible")

    fork = _fork_of(run)
    assert fork.review_state == SurveySubmission.REVIEW_NEW, "untouched"
    assert fork in SurveySubmission.objects.countable()


def test_a_standalone_survey_run_is_untouched_by_a_run_decision(submitter, reviewer):
    standalone = SurveySubmission.objects.create(
        origin=SurveySubmission.ORIGIN_SURVEY,
        trust_tier=SurveySubmission.TIER_VERIFIED,
        identity_hash="standalone",
    )
    run = _benchmark_run(submitter)

    run_services.reject_run(run, by=reviewer, reason="no")

    standalone.refresh_from_db()
    assert standalone.review_state == SurveySubmission.REVIEW_NEW


def test_rejecting_a_quarantined_run_leaves_its_fork_too(submitter, reviewer):
    run = _benchmark_run(submitter)
    run.status = TestRun.STATUS_QUARANTINED
    run.save(update_fields=["status"])

    run_services.reject_run(run, by=reviewer, reason="not AlmaLinux")

    assert _fork_of(run).review_state == SurveySubmission.REVIEW_NEW
    assert _fork_of(run) in SurveySubmission.objects.countable()


def test_a_census_failure_cannot_undo_a_run_decision(submitter, reviewer, monkeypatch):
    """The survey is a separate stream. A problem there must never roll back a decision a
    reviewer has already made about a certification run."""
    run = _benchmark_run(submitter)

    from lumina.survey import services as survey_services

    def boom(*args, **kwargs):
        raise RuntimeError("census exploded")

    monkeypatch.setattr(survey_services, "moderate_submission", boom)
    run_services.approve_run(run, by=reviewer)

    run.refresh_from_db()
    assert run.status == TestRun.STATUS_APPROVED


def test_a_rejected_run_that_is_later_approved_marks_its_fork_reviewed(submitter, reviewer):
    run = _benchmark_run(submitter)
    run_services.reject_run(run, by=reviewer, reason="mistake")
    assert _fork_of(run).review_state == SurveySubmission.REVIEW_NEW

    run.status = TestRun.STATUS_PENDING
    run.save(update_fields=["status"])
    run_services.approve_run(run, by=reviewer)

    assert _fork_of(run).review_state == SurveySubmission.REVIEW_ACCEPTED


def test_a_dismissal_is_still_possible_for_a_genuinely_bogus_fork(submitter, reviewer):
    """Nothing above makes a fork undismissable. It is not moderated from the survey queue,
    but an administrator can still exclude one from the Django admin, which is where a
    plainly bogus inventory gets dealt with."""
    from lumina.survey import services as survey_services

    run = _benchmark_run(submitter)
    fork = _fork_of(run)

    survey_services.moderate_submission(fork, by=reviewer, dismiss=True, via_run=True)

    fork.refresh_from_db()
    assert fork not in SurveySubmission.objects.countable()
