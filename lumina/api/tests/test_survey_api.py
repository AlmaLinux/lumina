"""The survey ingest endpoint: authenticated, submit-scoped, one submission per upload."""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework.test import APIClient

from lumina.accounts.models import ApiToken
from lumina.results.tests import factories as f
from lumina.survey.models import SurveySubmission

pytestmark = pytest.mark.django_db
User = get_user_model()
URL = reverse("survey-ingest")


def _client(scopes, username="fleet"):
    user = User.objects.create_user(username=username, password="x")
    _, raw = ApiToken.issue(user=user, name="survey", scopes=scopes)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
    return client, user


def _upload():
    return f.as_upload(f.build_bundle(f.make_report()))


def test_unauthenticated_upload_is_rejected():
    resp = APIClient().post(URL, {"bundle": _upload()}, format="multipart")
    assert resp.status_code == 401


def test_upload_requires_submit_scope():
    client, _ = _client([ApiToken.SCOPE_READ])
    resp = client.post(URL, {"bundle": _upload()}, format="multipart")
    assert resp.status_code == 403
    assert resp.json()["code"] == "insufficient_scope"


def test_submit_scoped_upload_creates_a_verified_submission():
    client, user = _client([ApiToken.SCOPE_SUBMIT])
    resp = client.post(URL, {"bundle": _upload()}, format="multipart")

    assert resp.status_code == 201, resp.content
    sub = SurveySubmission.objects.get()
    assert sub.trust_tier == SurveySubmission.TIER_VERIFIED
    assert sub.submitter == user
    assert sub.token is not None
    assert sub.source_ip_hash                       # IP recorded as a hash, never raw
    assert resp.json()["uuid"] == str(sub.uuid)


def test_missing_bundle_is_a_clean_400():
    client, _ = _client([ApiToken.SCOPE_SUBMIT])
    resp = client.post(URL, {}, format="multipart")
    assert resp.status_code == 400
    assert resp.json()["code"] == "missing_bundle"
