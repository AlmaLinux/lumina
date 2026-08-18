"""A run sent back for changes has to be actionable by its submitter.

"Needs changes" is a request *to the submitter*, so the whole loop has to close:
they must see what was asked, be able to edit it, and send it back. It used to
be a dead end - submit_for_review accepted drafts only, the edit controls
rendered for drafts only, and the reviewer's reason was never shown to them.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import Group, User
from django.urls import reverse

from lumina.results import ingest, services
from lumina.results.models import TestRun
from lumina.results.tests import factories as f
from lumina.results.tests.helpers import release
from lumina.vendors.models import Vendor

pytestmark = pytest.mark.django_db


@pytest.fixture
def submitter():
    return User.objects.create_user("nc-sub", email="nc@example.com")


@pytest.fixture
def reviewer():
    user = User.objects.create_user("nc-rev", email="ncr@example.com")
    group, _ = Group.objects.get_or_create(name="reviewer")
    user.groups.add(group)
    return user


def _bounced(submitter, reviewer, reason="Use the marketing name, not the MTM."):
    Vendor.objects.get_or_create(name="Dell Inc.", defaults={"slug": "dell-inc"})
    report = f.make_report(
        run_types=["validate"],
        results=[f.validate_result("validate.cpu.functional")],
    )
    run = ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(f.build_bundle(report)),
        source="api",
    )
    run.listing_proposal = {"vendor_name": "Dell Inc.", "name": "R760"}
    run.save(update_fields=["listing_proposal"])
    # release() is the stand-in for the submitter's own submit step.
    release(run)
    run.refresh_from_db()
    services.request_run_changes(run, by=reviewer, reason=reason)
    run.refresh_from_db()
    return run


def test_a_bounced_run_can_be_resubmitted(submitter, reviewer):
    run = _bounced(submitter, reviewer)
    assert run.status == TestRun.STATUS_NEEDS_CHANGES

    services.submit_for_review(run, by=submitter)

    run.refresh_from_db()
    assert run.status == TestRun.STATUS_PENDING


def test_an_approved_run_still_cannot_be_resubmitted(submitter, reviewer):
    run = _bounced(submitter, reviewer)
    services.submit_for_review(run, by=submitter)
    services.approve_run(run, by=reviewer)

    with pytest.raises(services.ReviewError, match="cannot be submitted"):
        services.submit_for_review(run, by=submitter)


def test_the_submitter_is_told_what_was_asked(client, submitter, reviewer):
    """Being told to change something without being told what is not a request."""
    run = _bounced(submitter, reviewer, reason="Use the marketing name, not the MTM.")
    client.force_login(submitter)

    body = client.get(run.get_absolute_url()).content.decode()

    assert "A reviewer asked for changes" in body
    assert "Use the marketing name, not the MTM." in body


def test_the_page_offers_edit_and_resubmit(client, submitter, reviewer):
    run = _bounced(submitter, reviewer)
    client.force_login(submitter)

    body = client.get(run.get_absolute_url()).content.decode()

    assert "Review and edit details" in body
    assert "Resubmit for review" in body
    # and exactly one route to the form, as on a draft
    assert body.count("propose-listing/") == 1


def test_the_form_is_editable_while_changes_are_pending(client, submitter, reviewer):
    run = _bounced(submitter, reviewer)
    client.force_login(submitter)

    resp = client.get(reverse("results:propose_listing", args=[run.uuid]))

    assert resp.status_code == 200


def test_editing_then_resubmitting_carries_the_correction(client, submitter,
                                                          reviewer):
    run = _bounced(submitter, reviewer)
    client.force_login(submitter)

    client.post(reverse("results:propose_listing", args=[run.uuid]), {
        "vendor_name": "Dell Inc.", "name": "PowerEdge R760",
        "machine_kind": "prebuilt", "model_number": "", "description": "",
        "vendor_spec_url": "", "cpu_model": "Xeon Gold 6430",
        "submitter_notes": "Renamed as asked.",
    })
    resp = client.post(reverse("results:submit_for_review", args=[run.uuid]))

    assert resp.status_code == 302
    run.refresh_from_db()
    assert run.status == TestRun.STATUS_PENDING
    assert run.listing_proposal["name"] == "PowerEdge R760"
    assert run.submitter_notes == "Renamed as asked."


def test_the_run_list_offers_the_action_explicitly(client, submitter, reviewer):
    """The machine name was already a link, but a row waiting on the submitter has to say so
    rather than leaving them to discover it.

    This had a twin pointed at a separate "my runs" page, which is gone: it listed the same runs
    with fewer columns and no actions, and this page is the better version of it.
    """
    _bounced(submitter, reviewer)
    client.force_login(submitter)

    body = client.get(reverse("accounts:dashboard")).content.decode()

    assert "Review and resubmit" in body


def test_a_resubmitted_run_is_back_in_the_review_queue(client, submitter, reviewer):
    run = _bounced(submitter, reviewer)
    services.submit_for_review(run, by=submitter)
    client.force_login(reviewer)

    body = client.get(reverse("review:queue")).content.decode()

    assert str(run.uuid)[:8] in body or run.display_name in body
