"""The submitter can review and correct the embargo before handing the run over.

Reported: "the form on the user side to set details before submitting for review should allow
the user to set/review the gating dates/status which may or may not have been set from the CLI
as intended."

Both values - ``pre_release`` and ``publish_requested_date`` - arrive once, at ingest, from the
CLI's run metadata or the web upload form. Until now the submitter could only *see* the outcome,
as a line on the run page reading "Embargoed until 2026-10-01". A flag missed on the command
line, a mistyped date, or hardware that stopped being unreleased between the run and the
submission each ended with the wrong thing happening publicly at approval, and nowhere to fix it
short of asking a reviewer.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth.models import User
from django.urls import reverse

from lumina.releases.models import AlmaLinuxRelease
from lumina.results import ingest
from lumina.results.forms import RunListingProposalForm
from lumina.results.tests import factories as f

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def release():
    AlmaLinuxRelease.objects.get_or_create(major=9, defaults={"supported": True})


@pytest.fixture
def submitter():
    return User.objects.create_user("emb-sub", password="pw")


def _run(submitter, **kwargs):
    return ingest.ingest_bundle(
        submitter=submitter, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"],
            results=[f.validate_result("validate.cpu.functional")],
        ))),
        **kwargs,
    )


def _post(client, run, **extra):
    data = {
        "vendor_name": "Dell Inc.", "name": "PowerEdge R760",
        "machine_kind": "prebuilt", "components_submitted": "1",
    }
    data.update(extra)
    return client.post(reverse("results:propose_listing", args=[run.uuid]), data)


def test_the_form_offers_both_controls(submitter):
    run = _run(submitter)

    form = RunListingProposalForm(run=run, user=submitter, subject="system")

    assert "pre_release" in form.fields
    assert "publish_requested_date" in form.fields


def test_what_the_cli_set_is_shown_rather_than_an_empty_pair(submitter):
    """The whole point of "review": a form that rendered both controls blank would imply nothing
    was set, which is how a wrong value survives to approval."""
    run = _run(submitter, pre_release=True)
    run.publish_requested_date = dt.date(2026, 10, 1)
    run.save(update_fields=["publish_requested_date"])

    initial = RunListingProposalForm.initial_from_run(run)

    assert initial["pre_release"] is True
    assert initial["publish_requested_date"] == dt.date(2026, 10, 1)


def test_the_page_says_the_run_is_embargoed(client, submitter):
    run = _run(submitter, pre_release=True)
    client.force_login(submitter)

    body = client.get(reverse("results:propose_listing", args=[run.uuid])).content.decode()

    assert "Publication" in body
    assert "marked as unreleased hardware" in body


def test_setting_the_embargo_lands_on_the_run(client, submitter):
    run = _run(submitter)
    assert run.pre_release is False, "the premise"
    client.force_login(submitter)

    _post(client, run, pre_release="on", publish_requested_date="2026-10-01")

    run.refresh_from_db()
    assert run.pre_release is True
    assert run.publish_requested_date == dt.date(2026, 10, 1)


def test_clearing_it_lands_too(client, submitter):
    """Hardware stops being unreleased. A control that could only be switched on would leave the
    submitter unable to correct the commoner mistake."""
    run = _run(submitter, pre_release=True)
    run.publish_requested_date = dt.date(2026, 10, 1)
    run.save(update_fields=["publish_requested_date"])
    client.force_login(submitter)

    _post(client, run)

    run.refresh_from_db()
    assert run.pre_release is False
    assert run.publish_requested_date is None


def test_a_date_without_the_flag_is_refused(client, submitter):
    """Silently ignoring it would surprise the submitter at exactly the wrong moment. The same
    rule ``BundleUploadForm`` applies - stated on both paths, because a rule enforced on one way
    in is not a rule."""
    run = _run(submitter)
    client.force_login(submitter)

    resp = _post(client, run, publish_requested_date="2026-10-01")

    assert resp.status_code == 200
    assert "Tick this to withhold the results" in resp.content.decode()
    run.refresh_from_db()
    assert run.publish_requested_date is None


def test_neither_value_reaches_the_listing_proposal(client, submitter):
    """They govern this evidence, not what the machine is. Five keys have leaked into that blob
    one at a time; these two are registered as controls where they are declared."""
    run = _run(submitter)
    client.force_login(submitter)

    _post(client, run, pre_release="on", publish_requested_date="2026-10-01")

    run.refresh_from_db()
    assert "pre_release" not in run.listing_proposal
    assert "publish_requested_date" not in run.listing_proposal
