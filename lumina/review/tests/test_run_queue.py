"""The validation & collect runs queue shows each run's pass/fail verdict.

A reviewer scanning the queue can see whether the tests passed on the machine (the run's own
verdict) without opening every row, kept distinct from the review Status beside it. A collect run
has no certification verdict, and neither does a benchmark run, so the column is shown only on the
validation/collect tab and reads blank for a collect row.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.urls import reverse

from lumina.results import ingest
from lumina.results.models import RunType, TestRun
from lumina.results.tests import factories as f

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture
def reviewer(client):
    u = User.objects.create_user(username="rev")
    u.groups.add(Group.objects.create(name="reviewer"))
    client.force_login(u)
    return u


@pytest.fixture
def submitter():
    return User.objects.create_user(username="sub")


def _pending_run(submitter, *, run_types, results) -> TestRun:
    run = ingest.ingest_bundle(
        submitter=submitter, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=run_types, results=results,
        ))),
    )
    run = TestRun.objects.get(pk=run.pk)
    # A validate run lands in DRAFT until the submitter finishes; force it into the review queue,
    # which is where this column lives. Collect/benchmark runs already start PENDING.
    run.status = TestRun.STATUS_PENDING
    run.save(update_fields=["status"])
    return run


def _queue_html(client) -> str:
    return client.get(reverse("review:queue")).content.decode()


class RunQueueVerdictTests:
    def test_a_passing_validate_run_shows_pass(self, client, reviewer, submitter):
        _pending_run(submitter, run_types=["validate"],
                     results=[f.validate_result("validate.cpu.functional", status="pass")])
        html = _queue_html(client)
        assert "PASS" in html
        # The header appears once: only the validation/collect tab carries the column.
        assert html.count("<th>Result</th>") == 1

    def test_a_failing_validate_run_shows_fail(self, client, reviewer, submitter):
        _pending_run(submitter, run_types=["validate"],
                     results=[f.validate_result("validate.cpu.functional", status="fail")])
        html = _queue_html(client)
        assert "FAIL" in html
        assert "PASS" not in html

    def test_the_result_column_is_not_on_the_benchmark_tab(self, client, reviewer, submitter):
        _pending_run(submitter, run_types=["benchmark"], results=[f.benchmark_result()])
        _pending_run(submitter, run_types=["validate"],
                     results=[f.validate_result("validate.cpu.functional")])
        html = _queue_html(client)
        # One column, on the validation/collect tab only - benchmark runs have no verdict.
        assert html.count("<th>Result</th>") == 1

    def test_the_verdict_costs_no_query_per_run(
        self, reviewer, submitter, django_assert_num_queries,
    ):
        """The badge calls verdict() on every row; the queue prefetches results so that is free.

        Pins the fix the verdict() docstring warns about: without the prefetch this is an EXISTS
        query per validate run.
        """
        for _ in range(3):
            _pending_run(submitter, run_types=["validate"],
                         results=[f.validate_result("validate.cpu.functional")])
        runs = list(
            TestRun.objects.open_for_review()
            .exclude(run_type=RunType.benchmark.value)
            .prefetch_related("results")
        )
        assert len(runs) == 3, "premise: three validate runs are in the queue"
        with django_assert_num_queries(0):
            [run.verdict() for run in runs]
