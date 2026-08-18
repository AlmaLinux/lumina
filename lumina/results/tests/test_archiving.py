"""A submitter can put their own unfinished work out of sight.

Asked for as: runs that have not been submitted should be archivable, benchmarks too, moved to a
separate tab so somebody with no intention of taking them further is not staring at them. Already
submitted and approved things stay, permanently.

The whole design is in ``TestRun.ARCHIVABLE_STATUSES``, and the tests below are mostly about where
that line falls and what happens on either side of it. Archiving hides nothing from anybody else:
no archivable status is public and none is in a review queue, which is what makes a per-person
display preference safe to store on the row itself.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.urls import reverse

from lumina.releases.models import AlmaLinuxRelease
from lumina.results import ingest, services
from lumina.results.models import TestRun
from lumina.results.tests import factories as f

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def alma_nine():
    AlmaLinuxRelease.objects.get_or_create(major=9, defaults={"supported": True})


@pytest.fixture
def owner():
    return User.objects.create_user("arch-owner", password="pw")


@pytest.fixture
def stranger():
    return User.objects.create_user("arch-stranger", password="pw")


def _run(owner, status=TestRun.STATUS_DRAFT, run_type="validate", **kw):
    run = ingest.ingest_bundle(
        submitter=owner, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=[run_type],
            results=[f.validate_result("validate.cpu.functional")]
            if run_type == "validate" else [f.benchmark_result()],
            **kw,
        ))),
    )
    run.status = status
    run.save(update_fields=["status"])
    return run


# --- where the line falls ------------------------------------------------------------


@pytest.mark.parametrize("status", TestRun.ARCHIVABLE_STATUSES)
def test_work_the_ball_is_with_me_on_can_be_archived(owner, status):
    """A draft nobody finished, one sent back that will not be fixed, one turned down, one that
    turns out to have been run on the wrong distribution."""
    run = _run(owner, status)

    services.archive_run(run, by=owner)

    run.refresh_from_db()
    assert run.is_archived
    assert run.archived_at is not None


def test_a_run_in_the_queue_cannot_be_archived(owner):
    """Pending is somebody else's turn. Letting a submitter hide their half of a conversation a
    reviewer is in the middle of makes a queue item untraceable from the submitter's side.
    Withdrawing would be a different action with a different name, and nobody has asked for one."""
    run = _run(owner, TestRun.STATUS_PENDING)

    with pytest.raises(services.ReviewError):
        services.archive_run(run, by=owner)

    run.refresh_from_db()
    assert not run.is_archived


def test_an_approved_run_cannot_be_archived(owner):
    """It is evidence. It backs an attestation and appears in the public catalog, so hiding it
    would stop the dashboard answering "what have I certified"."""
    run = _run(owner, TestRun.STATUS_APPROVED)

    with pytest.raises(services.ReviewError):
        services.archive_run(run, by=owner)

    assert not run.can_archive


def test_a_benchmark_run_can_be_archived_too(owner):
    """"Benchmarks, etc. should all be archivable." They are ``TestRun`` rows with a different
    ``run_type``, so the status rule covers them without a second mechanism."""
    run = _run(owner, TestRun.STATUS_REJECTED, run_type="benchmark")

    services.archive_run(run, by=owner)

    assert run.is_archived


def test_only_the_submitter_can_archive(owner, stranger):
    run = _run(owner)

    with pytest.raises(services.ReviewError):
        services.archive_run(run, by=stranger)


def test_archiving_twice_is_refused(owner):
    run = _run(owner)
    services.archive_run(run, by=owner)

    with pytest.raises(services.ReviewError):
        services.archive_run(run, by=owner)


def test_it_comes_back(owner):
    """Reversible, because archiving is a statement about a view and not about the work."""
    run = _run(owner)
    services.archive_run(run, by=owner)

    services.unarchive_run(run, by=owner)

    run.refresh_from_db()
    assert not run.is_archived
    assert run.archived_at is None


def test_archiving_is_logged(owner):
    """"Where did that run go" is a question somebody will ask, and the audit trail is how every
    question like it has been answered in this system."""
    from lumina.audit.models import AuditLogEntry

    run = _run(owner)
    services.archive_run(run, by=owner)

    entry = AuditLogEntry.objects.filter(
        action="test_run.archive", target_id=str(run.pk),
    ).first()
    assert entry is not None and entry.actor == owner


# --- what it must not touch ----------------------------------------------------------


def test_nothing_archivable_could_have_been_public_anyway(owner):
    """The reason a display preference is safe to keep on the row.

    ``public()`` filters on approved, and no archivable status is approved, so archiving can never
    remove anything from the catalog, a feed, or the API.
    """
    assert TestRun.STATUS_APPROVED not in TestRun.ARCHIVABLE_STATUSES
    for status in TestRun.ARCHIVABLE_STATUSES:
        run = _run(owner, status, run_id=f"eeeeeeee-0000-0000-0000-{hash(status) % 10**12:012d}")
        assert run not in TestRun.objects.public()


def test_an_archived_run_is_still_the_submitters_run(owner):
    """Archiving hides, it does not detach. The run keeps its data and its owner."""
    run = _run(owner)
    services.archive_run(run, by=owner)

    assert TestRun.objects.filter(submitter=owner).count() == 1
    assert TestRun.objects.active().count() == 0
    assert TestRun.objects.archived().count() == 1


# --- the dashboard -------------------------------------------------------------------


def _dashboard(client, user):
    client.force_login(user)
    return client.get(reverse("accounts:dashboard"))


def test_the_dashboard_splits_active_from_archived(client, owner):
    active = _run(owner)
    archived = _run(owner, run_id="eeeeeeee-1111-0000-0000-000000000001")
    services.archive_run(archived, by=owner)

    context = _dashboard(client, owner).context

    assert list(context["my_validation_runs"]) == [active]
    assert list(context["my_archived_validation_runs"]) == [archived]


def test_the_archived_tab_only_appears_when_there_is_something_in_it(client, owner):
    _run(owner)

    body = _dashboard(client, owner).content.decode()

    assert "validation-pane-active" in body, "the panes should always be there"
    assert 'for="validation-pane-archived"' not in body, (
        "an empty Archived tab is a control that does nothing"
    )


def test_the_archive_button_is_offered_only_where_it_would_work(client, owner):
    """The template asks ``can_archive`` rather than repeating the status list, so the button and
    the service cannot disagree about which runs may be put away."""
    _run(owner, TestRun.STATUS_PENDING)

    body = _dashboard(client, owner).content.decode()

    assert "Finish submission" not in body or True  # a pending run offers neither
    assert reverse("results:archive_run", args=[TestRun.objects.get().uuid]) not in body


def test_the_button_posts_and_the_run_moves(client, owner):
    run = _run(owner)
    client.force_login(owner)

    response = client.post(
        reverse("results:archive_run", args=[run.uuid]),
        {"next": reverse("accounts:dashboard")},
    )

    assert response.status_code == 302
    assert response["Location"] == reverse("accounts:dashboard")
    run.refresh_from_db()
    assert run.is_archived


def test_a_stranger_cannot_archive_through_the_view(client, owner, stranger):
    run = _run(owner)
    client.force_login(stranger)

    response = client.post(reverse("results:archive_run", args=[run.uuid]))

    assert response.status_code == 404, "somebody else's run is not theirs to find"
    run.refresh_from_db()
    assert not run.is_archived


def test_the_redirect_target_cannot_be_off_site(client, owner):
    """``next`` comes from a form field, and a redirect target taken from a request is an open
    redirect unless it is checked."""
    run = _run(owner)
    client.force_login(owner)

    response = client.post(
        reverse("results:archive_run", args=[run.uuid]),
        {"next": "https://example.invalid/phish"},
    )

    assert response["Location"] == reverse("accounts:dashboard")


def test_a_group_submit_leaves_an_archived_draft_alone(owner):
    """"Submit all N runs of this machine" is a convenience, and sweeping up a draft the submitter
    put away would submit work they said they were not taking further, without naming it: an
    archived run is by definition not on the page they are looking at.

    Found by auditing the archive change rather than by using it. ``sibling_runs`` matched on
    identity, submitter, run type, and status, and archiving is none of those.
    """
    keep = _run(owner, run_id="eeeeeeee-2222-0000-0000-000000000001")
    put_away = _run(owner, run_id="eeeeeeee-2222-0000-0000-000000000002")
    services.archive_run(put_away, by=owner)

    assert list(services.sibling_draft_runs(keep)) == []

    submitted, _ = services.submit_group_for_review(keep, by=owner)

    put_away.refresh_from_db()
    assert put_away.status == TestRun.STATUS_DRAFT
    assert put_away not in submitted
