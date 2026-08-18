"""The survey admin registers and its changelists render (catches admin misconfig)."""
from __future__ import annotations

import pytest
from django.contrib import admin as djadmin
from django.contrib.auth import get_user_model
from django.urls import reverse

from lumina.survey import models

pytestmark = pytest.mark.django_db
User = get_user_model()

_MODELS = (
    models.SurveySubmission,
    models.SurveyStat,
    models.SurveyTokenGrant,
    models.SurveyTokenRequest,
)


def test_all_survey_models_are_registered():
    for model in _MODELS:
        assert djadmin.site.is_registered(model)


def test_changelists_render(client):
    admin_user = User.objects.create_superuser(username="admin", password="x", email="a@b.c")
    client.force_login(admin_user)
    for model in _MODELS:
        opts = model._meta
        url = reverse(f"admin:{opts.app_label}_{opts.model_name}_changelist")
        assert client.get(url).status_code == 200, opts.model_name


def test_moderation_action_updates_review_state(client):
    admin_user = User.objects.create_superuser(username="admin", password="x", email="a@b.c")
    sub = models.SurveySubmission.objects.create(
        origin=models.SurveySubmission.ORIGIN_SURVEY,
        trust_tier=models.SurveySubmission.TIER_VERIFIED,
    )
    client.force_login(admin_user)
    client.post(
        reverse("admin:survey_surveysubmission_changelist"),
        {"action": "dismiss", "_selected_action": [sub.pk]},
    )
    sub.refresh_from_db()
    assert sub.review_state == models.SurveySubmission.REVIEW_DISMISSED
