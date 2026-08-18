"""Standalone survey ingest lands one append-only submission and no TestRun."""
from __future__ import annotations

import pytest

from lumina.results.ingest import InvalidReport
from lumina.results.models import TestRun
from lumina.results.tests import factories as f
from lumina.survey import ingest
from lumina.survey.models import SurveySubmission

pytestmark = pytest.mark.django_db


def test_ingest_survey_bundle_creates_one_submission_and_no_run():
    bundle = f.build_bundle(f.make_report())
    sub = ingest.ingest_survey_bundle(
        bundle_file=f.as_upload(bundle),
        trust_tier=SurveySubmission.TIER_VERIFIED,
    )

    assert sub.origin == SurveySubmission.ORIGIN_SURVEY
    assert sub.system_serial == "ABC1234"
    assert SurveySubmission.objects.count() == 1
    assert TestRun.objects.count() == 0     # the certification path is never touched


def test_survey_bundle_without_inventory_is_rejected():
    report = f.make_report()
    report["inventory"] = {}
    bundle = f.build_bundle(report)
    with pytest.raises(InvalidReport):
        ingest.ingest_survey_bundle(
            bundle_file=f.as_upload(bundle),
            trust_tier=SurveySubmission.TIER_VERIFIED,
        )
    assert not SurveySubmission.objects.exists()
