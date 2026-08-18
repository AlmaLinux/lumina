"""What the decision controls promise, against what the services actually do.

Two places where the page and the code had drifted, both found by walking the reviewer's flow
step by step rather than by reading either side on its own. Neither is a crash and neither shows
up in a response body assertion, because in both cases the words rendered fine and were wrong.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import Group, User
from django.urls import reverse

from lumina.releases.models import AlmaLinuxRelease
from lumina.results import ingest
from lumina.results.models import TestRun
from lumina.results.tests import factories as f
from lumina.results.tests.helpers import release as _ready
from lumina.vendors.models import Vendor

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def alma_nine():
    AlmaLinuxRelease.objects.get_or_create(major=9, defaults={"supported": True})
    Vendor.objects.get_or_create(name="Dell Inc.", defaults={"published": True})


@pytest.fixture
def submitter():
    return User.objects.create_user("dw-sub", password="pw")


@pytest.fixture
def reviewer():
    user = User.objects.create_user("dw-rev", password="pw")
    group, _ = Group.objects.get_or_create(name="reviewer")
    user.groups.add(group)
    return user


def _run(submitter, **report_kw):
    return ingest.ingest_bundle(
        submitter=submitter, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"],
            results=[f.validate_result("validate.cpu.functional")],
            **report_kw,
        ))),
    )


def _messages(response) -> list[str]:
    return [str(m) for m in response.wsgi_request._messages]


# --- the embargo label ---------------------------------------------------------------


def test_a_dateless_hold_is_labelled_embargoed(client, submitter, reviewer):
    """The label read ``pre_release and publish_requested_date``; ``approve_run`` embargoes on
    ``pre_release and (date is None or date > today)``. So the one case most in need of the
    warning, unreleased hardware whose announcement date is not settled, got a plain "Approve"
    button and then held the run anyway."""
    run = _run(submitter, pre_release=True)
    _ready(run, submitter)
    assert run.pre_release and run.publish_requested_date is None, "the premise"
    client.force_login(reviewer)

    body = client.get(reverse("review:run_detail", args=[run.pk])).content.decode()

    assert "Approve (embargoed)" in " ".join(body.split())


def test_a_dated_hold_is_still_labelled(client, submitter, reviewer):
    run = _run(submitter, pre_release=True, publish_after="2027-01-01")
    _ready(run, submitter)
    client.force_login(reviewer)

    body = client.get(reverse("review:run_detail", args=[run.pk])).content.decode()

    assert "Approve (embargoed)" in " ".join(body.split())


def test_an_ordinary_run_is_not_labelled(client, submitter, reviewer):
    run = _run(submitter)
    _ready(run, submitter)
    client.force_login(reviewer)

    body = client.get(reverse("review:run_detail", args=[run.pk])).content.decode()

    assert "(embargoed)" not in body


def test_a_dateless_hold_does_not_say_until_none(client, submitter, reviewer):
    """It said "Embargoed until None"."""
    run = _run(submitter, pre_release=True)
    _ready(run, submitter)
    client.force_login(reviewer)

    response = client.post(
        reverse("review:run_approve", args=[run.pk]), {"notes": ""}, follow=True,
    )

    said = " ".join(_messages(response))
    assert "None" not in said
    assert "no release date" in said.lower()


def test_a_dated_hold_still_names_the_date(client, submitter, reviewer):
    run = _run(submitter, pre_release=True, publish_after="2027-01-01")
    _ready(run, submitter)
    client.force_login(reviewer)

    response = client.post(
        reverse("review:run_approve", args=[run.pk]), {"notes": ""}, follow=True,
    )

    assert "2027-01-01" in " ".join(_messages(response))


# --- releasing a quarantine ------------------------------------------------------------


def test_releasing_a_validate_run_says_where_it_actually_went(client, submitter, reviewer):
    """It said the run was "back in the review queue". ``normal_initial_status`` returns
    ``STATUS_DRAFT`` for a validate run, because it still has no listing details, so it goes back
    to its submitter and only a benchmark run rejoins the queue. A reviewer was sent looking in a
    queue for something that was never going to be there."""
    run = _run(submitter, os_id="rocky")
    assert run.is_quarantined, "the premise"
    client.force_login(reviewer)

    response = client.post(
        reverse("review:run_release_quarantine", args=[run.pk]),
        {"reason": "os-release was misreported by the image"}, follow=True,
    )

    run.refresh_from_db()
    assert run.status == TestRun.STATUS_DRAFT
    said = " ".join(_messages(response))
    assert "review queue" not in said
    assert "submitter" in said


def test_releasing_a_benchmark_run_does_say_the_queue(client, submitter, reviewer):
    """The other branch, which was right all along and must stay right."""
    run = ingest.ingest_bundle(
        submitter=submitter, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["benchmark"], os_id="rocky",
            results=[f.benchmark_result()],
        ))),
    )
    assert run.is_quarantined, "the premise"
    client.force_login(reviewer)

    response = client.post(
        reverse("review:run_release_quarantine", args=[run.pk]),
        {"reason": "os-release was misreported by the image"}, follow=True,
    )

    run.refresh_from_db()
    assert run.status == TestRun.STATUS_PENDING
    assert "review queue" in " ".join(_messages(response))
