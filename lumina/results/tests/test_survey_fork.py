"""Ingesting a certification run forks an independent survey record.

Lives here rather than in ``survey/tests`` so it inherits the release-seeding
fixture the ingest path needs; the behavior under test is an ingest concern.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from lumina.results import ingest
from lumina.results.tests import factories as f
from lumina.survey.models import SurveySubmission

pytestmark = pytest.mark.django_db
User = get_user_model()


def test_ingesting_a_run_forks_a_verified_survey_record():
    submitter = User.objects.create_user(username="sub", password="x")
    bundle = f.build_bundle(f.make_report(run_types=["validate"], version_id="9.6"))

    run = ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(bundle), source="test",
    )

    sub = SurveySubmission.objects.get()
    assert sub.origin == SurveySubmission.ORIGIN_CERT_RUN
    assert sub.trust_tier == SurveySubmission.TIER_VERIFIED
    assert sub.submitter == submitter
    assert sub.system_serial == "ABC1234"      # raw identity captured from the bundle
    assert sub.cpu_model                        # facets extracted
    assert sub.inventory == run.inventory       # same verbatim inventory


def test_survey_disabled_forks_nothing(settings):
    settings.LUMINA_SURVEY_ENABLED = False
    submitter = User.objects.create_user(username="sub2", password="x")
    bundle = f.build_bundle(f.make_report(run_types=["benchmark"]))

    ingest.ingest_bundle(submitter=submitter, bundle_file=f.as_upload(bundle), source="test")

    assert not SurveySubmission.objects.exists()
