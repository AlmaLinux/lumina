"""A run can be published without its submitter's name on it.

Contributing evidence and being credited for it are separate decisions. Somebody
testing hardware they would rather not be publicly associated with - a customer's
machine, their own home lab - previously had one option, which was not to submit. So a
run can be listed as "Anonymous", set at run time with ``--anonymous``, defaulted
account-wide, answered again on the proposal form, and changed afterwards from the
dashboard.

Anonymity here is about *attribution*, not identity. The submitter is still on the run
and is still shown to reviewers, who have to know whose evidence they are judging. The
tests below pin both halves: what the public is shown, and what a reviewer still sees.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import Group, User
from django.urls import reverse
from django.utils import timezone

from lumina.accounts.models import AccountSettings
from lumina.results import ingest, services
from lumina.results.models import TestRun
from lumina.results.tests import factories as f

pytestmark = pytest.mark.django_db


@pytest.fixture
def submitter():
    return User.objects.create_user("anon-sub", email="as@example.com", password="x")


@pytest.fixture
def reviewer():
    user = User.objects.create_user("anon-rev", email="ar@example.com", password="x")
    user.groups.add(Group.objects.get_or_create(name="reviewer")[0])
    return user


def _ingest(submitter, *, anonymous=None, run_types=None):
    report = f.make_report(
        run_types=run_types or ["validate"],
        results=[f.validate_result("validate.cpu.functional")],
    )
    return ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(f.build_bundle(report)),
        source="api", publish_anonymously=anonymous,
    )


# --- what the flag decides -------------------------------------------------------

def test_the_public_name_is_anonymous_and_the_submitter_is_still_recorded(submitter):
    run = _ingest(submitter, anonymous=True)

    assert run.publish_anonymously
    assert run.public_submitter_name == "Anonymous"
    assert run.submitter == submitter  # the record itself is untouched


def test_an_attributed_run_publishes_the_username(submitter):
    run = _ingest(submitter)

    assert not run.publish_anonymously
    assert run.public_submitter_name == "anon-sub"


# --- where the answer comes from -------------------------------------------------

def test_the_account_default_applies_when_the_upload_says_nothing(submitter):
    AccountSettings.objects.create(user=submitter, publish_anonymously=True)

    assert _ingest(submitter).publish_anonymously


def test_an_explicit_answer_beats_the_account_default_in_both_directions(submitter):
    AccountSettings.objects.create(user=submitter, publish_anonymously=True)

    # The account says anonymous; this one run asked to be attributed.
    assert not _ingest(submitter, anonymous=False).publish_anonymously

    AccountSettings.objects.filter(user=submitter).update(publish_anonymously=False)

    # And the reverse: one anonymous run from an account that normally publishes a name.
    assert _ingest(submitter, anonymous=True).publish_anonymously


def test_an_account_that_never_set_anything_publishes_a_name(submitter):
    # Reading the default must not write a settings row: submitting a run is not
    # the same act as saving a preference.
    assert not _ingest(submitter).publish_anonymously
    assert not AccountSettings.objects.filter(user=submitter).exists()


# --- the public surfaces ---------------------------------------------------------

def test_the_run_page_shows_anonymous_to_the_public(client, submitter, reviewer):
    run = _ingest(submitter, anonymous=True)
    run.status = TestRun.STATUS_APPROVED
    run.published_at = run.received_at
    run.save(update_fields=["status", "published_at"])

    body = client.get(run.get_absolute_url()).content.decode()

    assert "Anonymous" in body
    assert "anon-sub" not in body


def test_the_api_does_not_expose_the_username(client, submitter):
    run = _ingest(submitter, anonymous=True)
    run.status = TestRun.STATUS_APPROVED
    run.published_at = run.received_at
    run.save(update_fields=["status", "published_at"])

    payload = client.get("/api/v1/results/").json()
    rows = payload["results"] if isinstance(payload, dict) else payload
    row = next(r for r in rows if r["uuid"] == str(run.uuid))

    assert row["submitter"] == "Anonymous"


def test_a_reviewer_still_sees_who_submitted_it(client, submitter, reviewer):
    run = _ingest(submitter, anonymous=True)
    run.status = TestRun.STATUS_PENDING
    run.save(update_fields=["status"])
    client.force_login(reviewer)

    body = client.get(reverse("review:run_detail", args=[run.pk])).content.decode()

    assert "anon-sub" in body, "a reviewer judging evidence must know whose it is"


# --- changing it later -----------------------------------------------------------

def test_the_submitter_can_anonymize_and_reattribute_a_published_run(client, submitter):
    run = _ingest(submitter)
    run.status = TestRun.STATUS_APPROVED
    run.published_at = run.received_at
    run.save(update_fields=["status", "published_at"])
    client.force_login(submitter)
    url = reverse("results:set_run_anonymity", args=[run.uuid])

    client.post(url, {"anonymous": "true"})
    run.refresh_from_db()
    assert run.publish_anonymously

    client.post(url, {"anonymous": "false"})
    run.refresh_from_db()
    assert not run.publish_anonymously


def test_nobody_else_can_change_how_a_run_is_attributed(client, submitter, reviewer):
    run = _ingest(submitter, anonymous=True)
    client.force_login(reviewer)

    resp = client.post(reverse("results:set_run_anonymity", args=[run.uuid]),
                       {"anonymous": "false"})

    assert resp.status_code == 404  # not theirs to answer
    run.refresh_from_db()
    assert run.publish_anonymously


def test_the_change_is_logged_against_the_run(submitter):
    from lumina.audit.models import AuditLogEntry

    run = _ingest(submitter)
    services.set_run_anonymity(run, by=submitter, anonymous=True)

    assert AuditLogEntry.objects.filter(action="test_run.set_anonymity").exists()


# --- the account-wide setting ----------------------------------------------------

def test_saving_the_preference_changes_only_future_runs(client, submitter):
    existing = _ingest(submitter)
    client.force_login(submitter)

    client.post(reverse("accounts:settings"), {"publish_anonymously": "on"})

    existing.refresh_from_db()
    assert not existing.publish_anonymously, "already-published work is not rewritten silently"
    assert AccountSettings.for_user(submitter).publish_anonymously
    assert _ingest(submitter).publish_anonymously  # but the next one is


def test_applying_to_existing_runs_is_a_separate_deliberate_tick(client, submitter):
    one, two = _ingest(submitter), _ingest(submitter)
    client.force_login(submitter)

    client.post(reverse("accounts:settings"),
                {"publish_anonymously": "on", "apply_to_existing": "on"})

    one.refresh_from_db()
    two.refresh_from_db()
    assert one.publish_anonymously and two.publish_anonymously


def test_turning_it_off_and_applying_reattributes_everything(client, submitter):
    run = _ingest(submitter, anonymous=True)
    client.force_login(submitter)

    client.post(reverse("accounts:settings"), {"apply_to_existing": "on"})

    run.refresh_from_db()
    assert not run.publish_anonymously


def test_the_settings_page_needs_a_login(client):
    resp = client.get(reverse("accounts:settings"))

    assert resp.status_code == 302
    assert "/login" in resp["Location"] or "oidc" in resp["Location"]


def test_the_toggle_is_on_the_owners_run_page_only(client, submitter, reviewer):
    run = _ingest(submitter)
    other = User.objects.create_user("passer-by", password="x")

    client.force_login(submitter)
    assert "List this run anonymously instead" in client.get(
        run.get_absolute_url()).content.decode()

    client.force_login(other)
    assert "List this run anonymously instead" not in client.get(
        run.get_absolute_url()).content.decode()


def test_the_review_page_shows_the_name_and_offers_no_toggle(client, submitter, reviewer):
    # The review page includes the same summary partial. A reviewer must see whose
    # evidence this is, and must not be able to change how it is attributed.
    run = _ingest(submitter, anonymous=True)
    run.status = TestRun.STATUS_PENDING
    run.save(update_fields=["status"])
    client.force_login(reviewer)

    body = client.get(reverse("review:run_detail", args=[run.pk])).content.decode()

    assert "anon-sub" in body
    assert "List this run anonymously instead" not in body


# --- the toggle has to describe the run it is on --------------------------------


def test_a_named_run_is_not_told_it_is_anonymous(client, submitter):
    """The two explanations were transposed in one branch, so a run published under its
    submitter's name said "Published as Anonymous" directly beneath that name."""
    run = _ingest(submitter)
    client.force_login(submitter)

    body = client.get(run.get_absolute_url()).content.decode()

    assert "Published under your username" in body
    assert "Published as “Anonymous”" not in body


def test_an_anonymous_run_says_so(client, submitter):
    run = _ingest(submitter, anonymous=True)
    client.force_login(submitter)

    body = client.get(run.get_absolute_url()).content.decode()

    assert "Published as “Anonymous”" in body
    assert "Published under your username" not in body


def test_the_reviewer_caveat_is_shown_before_the_decision_not_after(client, submitter):
    """It is what somebody weighing anonymity needs to know, and it used to appear only once
    they were already anonymous."""
    run = _ingest(submitter)
    client.force_login(submitter)

    body = client.get(run.get_absolute_url()).content.decode()

    assert "reviewers and administrators still see who submitted it" in body.lower()


# --- a reviewer is told both things ----------------------------------------------


def test_a_reviewer_sees_the_real_name_in_the_summary_panel(client, submitter, reviewer):
    """The panel read "Anonymous" to reviewers too, which is not what the flag means: the promise
    is that the *public* does not see the submitter, and a reviewer judging evidence has to know
    whose it is."""
    run = _ingest(submitter, anonymous=True)
    run.status = TestRun.STATUS_PENDING
    run.save(update_fields=["status"])
    client.force_login(reviewer)

    body = client.get(reverse("review:run_detail", args=[run.pk])).content.decode()

    assert "anon-sub" in body
    assert "Published as Anonymous" in body, "and that it is anonymous, which is also a fact"


def test_the_public_still_sees_only_anonymous(client, submitter):
    """The other half. A name shown to a passer-by is the leak this flag exists to prevent."""
    run = _ingest(submitter, anonymous=True)
    # Published, because a draft is not visible to a passer-by at all and the question here is
    # what a passer-by is shown, not whether they get in.
    TestRun.objects.filter(pk=run.pk).update(
        status=TestRun.STATUS_APPROVED, published_at=timezone.now())
    client.force_login(User.objects.create_user("passer-by-2", password="x"))

    body = client.get(run.get_absolute_url()).content.decode()

    assert "anon-sub" not in body
    assert TestRun.ANONYMOUS_LABEL in body


def test_the_submitter_sees_their_own_run_as_the_public_does(client, submitter):
    """They are the one person who already knows, and the page is showing them what it publishes."""
    run = _ingest(submitter, anonymous=True)
    client.force_login(submitter)

    body = client.get(run.get_absolute_url()).content.decode()

    assert "Published as Anonymous" not in body
    assert TestRun.ANONYMOUS_LABEL in body


def test_a_named_run_gets_no_marker_for_anybody(client, submitter, reviewer):
    run = _ingest(submitter)
    run.status = TestRun.STATUS_PENDING
    run.save(update_fields=["status"])
    client.force_login(reviewer)

    body = client.get(reverse("review:run_detail", args=[run.pk])).content.decode()

    assert "anon-sub" in body
    assert "Published as Anonymous" not in body
