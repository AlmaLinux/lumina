"""Validation runs are completed by their submitter before review.

The suite cannot know a system's marketing name, description, or spec-sheet
link, so a validation run lands as a draft. The submitter fills those in and
releases it. Benchmark and collect runs need nothing extra and skip this.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import Group, User
from django.urls import reverse

from lumina.hardware.models import System
from lumina.results import ingest, services
from lumina.results.models import TestRun
from lumina.results.tests import factories as f
from lumina.vendors.models import Vendor

pytestmark = pytest.mark.django_db


@pytest.fixture
def submitter():
    return User.objects.create_user("runner")


@pytest.fixture
def reviewer():
    user = User.objects.create_user("rev")
    group, _ = Group.objects.get_or_create(name="reviewer")
    user.groups.add(group)
    return user


def _ingest(submitter, **kw):
    report = f.make_report(**kw)
    return ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(f.build_bundle(report)),
        source="api",
    )


VALIDATE = {"run_types": ["validate"],
            "results": [f.validate_result("validate.cpu.functional")]}


# --- initial state ------------------------------------------------------------


def test_validation_run_lands_as_draft(submitter):
    run = _ingest(submitter, **VALIDATE)
    assert run.status == TestRun.STATUS_DRAFT


def test_benchmark_run_goes_straight_to_pending(submitter):
    run = _ingest(submitter, run_types=["benchmark"],
                  results=[f.benchmark_result()])
    assert run.status == TestRun.STATUS_PENDING


def test_collect_run_goes_straight_to_pending(submitter):
    run = _ingest(submitter, run_types=["collect"])
    assert run.status == TestRun.STATUS_PENDING


# --- drafts are nobody else's business ----------------------------------------


def test_draft_is_not_in_the_review_queue(submitter):
    run = _ingest(submitter, **VALIDATE)
    assert run not in TestRun.objects.open_for_review()
    assert run in TestRun.objects.drafts_for(submitter)


def test_reviewer_cannot_approve_a_draft(submitter, reviewer):
    run = _ingest(submitter, **VALIDATE)
    with pytest.raises(services.ReviewError, match="not finished"):
        services.approve_run(run, by=reviewer)


def test_reviewer_cannot_reject_a_draft(submitter, reviewer):
    run = _ingest(submitter, **VALIDATE)
    with pytest.raises(services.ReviewError):
        services.reject_run(run, by=reviewer)


def test_draft_is_never_public(submitter):
    run = _ingest(submitter, **VALIDATE)
    assert run not in TestRun.objects.public()


# --- completing and releasing --------------------------------------------------


def test_new_model_must_supply_listing_details(submitter):
    run = _ingest(submitter, **VALIDATE)
    assert services.missing_submission_details(run)
    with pytest.raises(services.ReviewError, match="Still needed"):
        services.submit_for_review(run, by=submitter)


def test_supplying_the_proposal_unblocks_release(submitter):
    run = _ingest(submitter, **VALIDATE)
    run.listing_proposal = {"vendor_name": "Dell Inc.", "name": "PowerEdge R760"}
    run.save()

    services.submit_for_review(run, by=submitter)

    run.refresh_from_db()
    assert run.status == TestRun.STATUS_PENDING
    assert run in TestRun.objects.open_for_review()


def test_already_cataloged_system_needs_nothing_extra(submitter):
    """Re-validating known hardware should not nag: the listing it will
    attest already carries the descriptive fields."""
    dell = Vendor.objects.create(name="Dell Inc.")
    System.objects.create(vendor=dell, name="PowerEdge R760")
    run = _ingest(submitter, **VALIDATE)

    assert services.missing_submission_details(run) == []
    services.submit_for_review(run, by=submitter)
    run.refresh_from_db()
    assert run.status == TestRun.STATUS_PENDING


def test_custom_build_must_describe_its_motherboard(submitter):
    """A custom build's motherboard *is* its listing, so it owes details like
    any other new hardware. Exempting custom builds let brand-new machines
    reach review with nothing a human had supplied."""
    run = _ingest(submitter, inventory=f.custom_build_inventory(), **VALIDATE)

    outstanding = services.missing_submission_details(run)
    assert outstanding, "a new custom build should be asked for details"
    assert "motherboard" in outstanding[0]
    with pytest.raises(services.ReviewError, match="Still needed"):
        services.submit_for_review(run, by=submitter)


def test_custom_build_proceeds_once_the_board_is_described(submitter):
    run = _ingest(submitter, inventory=f.custom_build_inventory(), **VALIDATE)
    run.listing_proposal = {"vendor_name": "ASRock", "name": "B650M PG Riptide"}
    run.save(update_fields=["listing_proposal"])

    assert services.missing_submission_details(run) == []
    services.submit_for_review(run, by=submitter)
    run.refresh_from_db()
    assert run.status == TestRun.STATUS_PENDING


def test_custom_build_needs_nothing_when_its_board_is_already_cataloged(submitter):
    """Re-validating a known board asks for nothing, same as a known system."""
    from lumina.hardware.models import Component, ComponentKind, ComponentRole

    asrock = Vendor.objects.create(name="ASRock")
    Component.objects.create(
        vendor=asrock, name="B650M PG Riptide", kind=ComponentKind.motherboard.value,
        role=ComponentRole.MODEL, slug="asrock-b650m-pg-riptide",
    )
    run = _ingest(submitter, inventory=f.custom_build_inventory(), **VALIDATE)

    assert services.missing_submission_details(run) == []


def test_release_is_idempotent_guarded(submitter):
    dell = Vendor.objects.create(name="Dell Inc.")
    System.objects.create(vendor=dell, name="PowerEdge R760")
    run = _ingest(submitter, **VALIDATE)
    services.submit_for_review(run, by=submitter)
    with pytest.raises(services.ReviewError, match="cannot be submitted"):
        services.submit_for_review(run, by=submitter)


def test_release_links_a_system_cataloged_after_ingest(submitter):
    """The catalog can gain the listing between ingest and release."""
    run = _ingest(submitter, **VALIDATE)
    assert run.listing_system is None
    dell = Vendor.objects.create(name="Dell Inc.")
    system = System.objects.create(vendor=dell, name="PowerEdge R760")

    services.submit_for_review(run, by=submitter)

    run.refresh_from_db()
    assert run.listing_system == system


# --- the web flow --------------------------------------------------------------


def test_run_page_shows_the_finish_prompt_to_its_submitter(client, submitter):
    run = _ingest(submitter, **VALIDATE)
    client.force_login(submitter)
    resp = client.get(run.get_absolute_url())
    assert "Finish your submission" in resp.text
    assert "Submit for review" in resp.text


def test_submit_button_posts_and_queues_the_run(client, submitter):
    dell = Vendor.objects.create(name="Dell Inc.")
    System.objects.create(vendor=dell, name="PowerEdge R760")
    run = _ingest(submitter, **VALIDATE)
    client.force_login(submitter)

    resp = client.post(reverse("results:submit_for_review", args=[run.uuid]))

    assert resp.status_code == 302
    run.refresh_from_db()
    assert run.status == TestRun.STATUS_PENDING


def test_only_the_submitter_can_release_their_run(client, submitter):
    run = _ingest(submitter, **VALIDATE)
    other = User.objects.create_user("someone-else")
    client.force_login(other)
    resp = client.post(reverse("results:submit_for_review", args=[run.uuid]))
    assert resp.status_code == 404
    run.refresh_from_db()
    assert run.status == TestRun.STATUS_DRAFT


def test_incomplete_release_attempt_is_refused_with_a_reason(client, submitter):
    run = _ingest(submitter, **VALIDATE)
    client.force_login(submitter)
    client.post(reverse("results:submit_for_review", args=[run.uuid]), follow=True)
    run.refresh_from_db()
    assert run.status == TestRun.STATUS_DRAFT


def test_api_reports_the_draft_status_so_the_cli_can_explain(client, submitter):
    """alma-cert prints the "not yet submitted" notice off this field."""
    from lumina.accounts.models import ApiToken

    _, raw = ApiToken.issue(user=submitter, name="t", scopes=["submit"])
    bundle = f.as_upload(f.build_bundle(f.make_report(**VALIDATE)))
    resp = client.post(
        "/api/v1/results/", {"bundle": bundle},
        HTTP_AUTHORIZATION=f"Bearer {raw}",
    )
    assert resp.status_code == 201
    assert resp.json()["status"] == TestRun.STATUS_DRAFT
