"""Hardware proved on AlmaLinux Kitten publishes with a disclaimer rather than being held back.

The case: Kitten carries hardware enablement before it reaches a shipped minor. A run on Kitten
genuinely proves the major works, but somebody installing AlmaLinux 10.1 today would find the
machine unsupported, so the catalog cannot simply say "AlmaLinux 10" and stop.

Decided against holding the entry: a reader looking for this machine is better served by "works
from 10.3" than by an entry that is not there, and the disclaimer is true whether or not the
minor has shipped. The alternative - withholding until a scheduled job notices - makes a correct
claim depend on a cron having run.

**The two gates are separate and compose.** ``pre_release`` is about secrecy: unannounced
hardware, nothing public until a date. ``available_from_minor`` is about timing: public hardware,
real evidence, support not in a shipped minor yet. A Kitten run on unreleased hardware carries
both, and each lifts on its own schedule.

**Nothing is scheduled for the lift.** ``AlmaLinuxRelease.latest_minor`` is admin-maintained and
the disclaimer is derived when a page is rendered, so raising that one number clears the note
from every listing waiting on that minor.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth.models import Group, User
from django.urls import reverse

from lumina.hardware.models import System
from lumina.releases.models import AlmaLinuxRelease
from lumina.results import ingest, services
from lumina.results.forms import RunListingProposalForm
from lumina.results.models import TestRun
from lumina.results.tests import factories as f
from lumina.results.tests.helpers import release as ready
from lumina.vendors.models import Vendor

pytestmark = pytest.mark.django_db

KITTEN = "AlmaLinux Kitten 10 (Purple Lion)"


@pytest.fixture(autouse=True)
def releases():
    """Released majors, stated explicitly.

    ``latest_minor`` defaults to empty, which now means "this major has not been released at
    all" - the state a Kitten-tracked major sits in for months. That is the right default for a
    new row and the wrong one for a fixture about shipped releases, so both are set here and the
    unreleased case gets its own tests below.
    """
    for major, latest_minor in ((9, 6), (10, 0)):
        release, _ = AlmaLinuxRelease.objects.get_or_create(
            major=major, defaults={"supported": True},
        )
        release.latest_minor = latest_minor
        release.save(update_fields=["latest_minor"])


@pytest.fixture
def submitter():
    return User.objects.create_user("kit-sub", password="pw")


@pytest.fixture
def reviewer():
    user = User.objects.create_user("kit-rev", password="pw")
    group, _ = Group.objects.get_or_create(name="reviewer")
    user.groups.add(group)
    return user


def _run(submitter, *, kitten=False, version_id="9.6"):
    run = ingest.ingest_bundle(
        submitter=submitter, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"], version_id=version_id,
            results=[f.validate_result("validate.cpu.functional")],
        ))),
    )
    if kitten:
        environment = dict(run.environment or {})
        environment["os"] = {**(environment.get("os") or {}), "pretty_name": KITTEN}
        run.environment = environment
        run.save(update_fields=["environment"])
    return TestRun.objects.get(pk=run.pk)


# --- recognising Kitten -----------------------------------------------------------


def test_kitten_is_recognised_from_the_reported_string(submitter):
    """Derived from ``PRETTY_NAME``, not from a flag stored at ingest. Kitten's naming is not
    ours to control, so a stored boolean would freeze today's reading into every old bundle."""
    assert _run(submitter, kitten=True).ran_on_prerelease_os is True


def test_a_shipped_release_is_not_kitten(submitter):
    assert _run(submitter).ran_on_prerelease_os is False


def test_a_report_without_the_field_is_not_kitten(submitter):
    """Bundles predating the suite change carry no ``pretty_name``. Hardware is gated on
    evidence that it was pre-release, never on an absence."""
    run = _run(submitter)
    run.environment = {"os": {"id": "almalinux", "version_id": "9.6"}}
    run.save(update_fields=["environment"])

    assert TestRun.objects.get(pk=run.pk).ran_on_prerelease_os is False


# --- who is asked --------------------------------------------------------------


def test_the_submitter_is_asked_only_after_a_kitten_run(submitter):
    """Reported rule: "we should only prompt this from the user side if the run was on almalinux
    kitten. If it's already on a stable release and the tests pass, there's nothing to gate."

    Absent rather than blank, because a blank box invites an answer and any answer would put a
    disclaimer on a claim a shipped release has already proved.
    """
    kitten = RunListingProposalForm(
        run=_run(submitter, kitten=True), user=submitter, subject="system",
    )
    stable = RunListingProposalForm(
        run=_run(submitter), user=submitter, subject="system",
    )

    assert "available_from_minor" in kitten.fields
    assert "available_from_minor" not in stable.fields


def test_the_stable_run_page_does_not_show_it_at_all(client, submitter):
    """Asserted on the rendered page, not only on ``form.fields``.

    The two are not the same question, as today has shown twice over: a field can be in the
    markup and invisible behind a disclosure, and a field can be absent from a form and its label
    still appear in surrounding copy. What was asked for is that a submitter on a stable run does
    not *see* this, so that is what this checks.
    """
    run = _run(submitter)
    client.force_login(submitter)

    body = client.get(reverse("results:propose_listing", args=[run.uuid])).content.decode()

    assert 'name="available_from_minor"' not in body
    assert "Support starts in minor" not in body
    # The rest of the card is still there: the embargo is every submitter's to set.
    assert 'name="pre_release"' in body


def test_the_kitten_run_page_does_show_it(client, submitter):
    run = _run(submitter, kitten=True)
    client.force_login(submitter)

    body = client.get(reverse("results:propose_listing", args=[run.uuid])).content.decode()

    assert 'name="available_from_minor"' in body
    assert "Support starts in minor" in body


def test_the_reviewer_is_asked_regardless(submitter):
    """The backstop. A submitter may leave it unset or set it wrongly, and by the time a reviewer
    is looking the run is no longer the submitter's to edit."""
    from lumina.results.forms import RunListingAssignForm

    form = RunListingAssignForm(run=_run(submitter))

    assert "available_from_minor" in form.fields


def test_the_form_says_what_has_already_shipped(submitter):
    """Context, not a prefilled guess.

    Defaulting to "the next minor" would put a plausible number in a box whose value adds a
    disclaimer to a public listing, which is the same trap as prefilling identity fields over an
    existing catalog entry: a default that looks right invites being accepted unread. The
    submitter knows which minor carries their patch; what they may not know is where the release
    stream has reached.

    This is also what the short-lived ``kitten_target_major`` checkbox was gesturing at. That
    flag recorded which major Kitten tracks, was read by nothing, and claimed in its help text to
    drive matching that actually comes from the run's own ``VERSION_ID``.
    """
    AlmaLinuxRelease.objects.filter(major=10).update(latest_minor=1)
    run = _run(submitter, kitten=True, version_id="10.0")

    field = RunListingProposalForm(
        run=run, user=submitter, subject="system",
    ).fields["available_from_minor"]

    assert "newest shipped minor of AlmaLinux 10 is 10.1" in field.help_text
    assert field.initial is None, "no guessed default"


def test_the_submitters_page_explains_why_it_is_there(client, submitter):
    run = _run(submitter, kitten=True)
    client.force_login(submitter)

    body = " ".join(
        client.get(reverse("results:propose_listing", args=[run.uuid]))
        .content.decode().split()
    )

    assert "AlmaLinux Kitten" in body
    assert 'name="available_from_minor"' in body


def test_a_reviewer_can_set_it(client, submitter, reviewer):
    run = _run(submitter, kitten=True)
    client.force_login(reviewer)

    client.post(reverse("review:run_assign_listing", args=[run.pk]), {
        "available_from_minor": "3",
    })

    run.refresh_from_db()
    assert run.available_from_minor == 3


def test_a_reviewer_can_clear_one_that_should_not_be_there(client, submitter, reviewer):
    """A blank box on their form is a decision, not silence - which is why the service takes an
    explicit "set it" flag rather than treating None as "did not say"."""
    run = _run(submitter, kitten=True)
    run.available_from_minor = 3
    run.save(update_fields=["available_from_minor"])
    client.force_login(reviewer)

    client.post(reverse("review:run_assign_listing", args=[run.pk]), {})

    run.refresh_from_db()
    assert run.available_from_minor is None


# --- what approval records ------------------------------------------------------


def _approved(submitter, reviewer, *, minor=None, kitten=True):
    dell, _ = Vendor.objects.get_or_create(name="Dell Inc.", defaults={"published": True})
    System.objects.create(vendor=dell, name="PowerEdge R760")
    run = _run(submitter, kitten=kitten, version_id="10.0")
    run.available_from_minor = minor
    run.save(update_fields=["available_from_minor"])
    services.approve_run(ready(run), by=reviewer)
    run.refresh_from_db()
    return run


def test_approval_carries_the_gate_onto_the_listing(submitter, reviewer):
    run = _approved(submitter, reviewer, minor=3)

    version = run.listing_system.versions.get(release__major=10)

    assert version.available_from_minor == 3


def test_the_listing_is_published_anyway(submitter, reviewer):
    """The whole decision. Holding it back would hide the one fact a reader wants."""
    run = _approved(submitter, reviewer, minor=3)

    assert run.listing_system.published is True


def test_the_major_badge_counts_a_kitten_attestation(submitter, reviewer):
    """Asked and answered directly: "a kitten-only attestation should count towards the major
    version badge, yes." The evidence is real; only its availability is in the future."""
    run = _approved(submitter, reviewer, minor=3)

    version = run.listing_system.versions.get(release__major=10)
    assert version.validation_level == "community"
    assert run.listing_system.validation_level == "community"
    assert run.listing_system.attestation_count == 1


# --- the disclaimer, and what lifts it -------------------------------------------


def test_the_disclaimer_names_the_major_kitten_and_the_minor(submitter, reviewer):
    """Wording per the decision: major support confirmed using Kitten, enablement landing in
    major.minor. All three, because "not yet available" alone reads as a failure."""
    run = _approved(submitter, reviewer, minor=3)

    version = run.listing_system.versions.get(release__major=10)

    assert version.pending_minor == 3
    assert version.disclaimer == (
        "AlmaLinux 10 support confirmed using AlmaLinux Kitten. "
        "The hardware enablement lands in AlmaLinux 10.3."
    )


def test_raising_the_shipped_minor_lifts_it(submitter, reviewer):
    """The one admin action, and nothing is scheduled: the note is derived when the page is
    rendered, so it clears from every listing waiting on that minor at once."""
    run = _approved(submitter, reviewer, minor=3)
    version = run.listing_system.versions.get(release__major=10)
    assert version.pending_minor == 3, "the premise"

    AlmaLinuxRelease.objects.filter(major=10).update(latest_minor=3)

    version.refresh_from_db()
    assert version.pending_minor is None
    assert version.disclaimer == ""
    assert version.available_from_minor == 3, "the record of where it landed is kept"


def test_a_run_on_a_shipped_release_carries_no_gate(submitter, reviewer):
    run = _approved(submitter, reviewer, minor=None, kitten=False)

    version = run.listing_system.versions.get(release__major=10)

    assert version.pending_minor is None
    assert version.disclaimer == ""


def test_the_catalog_page_shows_it(client, submitter, reviewer):
    run = _approved(submitter, reviewer, minor=3)

    body = " ".join(
        client.get(reverse("hardware:detail", args=[run.listing_system.slug]))
        .content.decode().split()
    )

    assert "from 10.3" in body
    assert "confirmed using AlmaLinux Kitten" in body


def test_the_api_publishes_it(client, submitter, reviewer):
    run = _approved(submitter, reviewer, minor=3)

    body = client.get(f"/api/v1/systems/{run.listing_system.slug}/").json()

    row = next(r for r in body["compatibility"] if r["major"] == 10)
    assert row["pending_minor"] == 3
    assert "AlmaLinux Kitten" in row["disclaimer"]


# --- a gate only ever loosens ----------------------------------------------------


def test_evidence_from_a_shipped_release_removes_the_gate(submitter, reviewer):
    """Somebody has now proved the machine works on something people can install, which
    supersedes the Kitten claim outright."""
    run = _approved(submitter, reviewer, minor=3)
    listing = run.listing_system

    later = _run(submitter, version_id="10.0")
    later.listing_system = listing
    later.save(update_fields=["listing_system"])
    services.approve_run(ready(later), by=reviewer)

    assert listing.versions.get(release__major=10).available_from_minor is None


def test_a_later_kitten_run_does_not_tighten_it(submitter, reviewer):
    """The earlier minor is the better news and the one already proved. Tightening would put a
    disclaimer back on a claim that had earned its way out of one."""
    run = _approved(submitter, reviewer, minor=1)
    listing = run.listing_system

    later = _run(submitter, kitten=True, version_id="10.0")
    later.listing_system = listing
    later.available_from_minor = 4
    later.save(update_fields=["listing_system", "available_from_minor"])
    services.approve_run(ready(later), by=reviewer)

    assert listing.versions.get(release__major=10).available_from_minor == 1


# --- the two gates are independent ------------------------------------------------


def test_both_gates_can_be_in_force_at_once(submitter, reviewer):
    """Reported requirement: "we could have version-gating due to a run on kitten, and still have
    pre-release/embargoed hardware gating. Both mechanisms need to work independently."""
    dell, _ = Vendor.objects.get_or_create(name="Dell Inc.", defaults={"published": True})
    System.objects.create(vendor=dell, name="PowerEdge R760")
    run = _run(submitter, kitten=True, version_id="10.0")
    run.available_from_minor = 3
    run.pre_release = True
    run.publish_requested_date = dt.date(2099, 1, 1)
    run.save(update_fields=["available_from_minor", "pre_release",
                            "publish_requested_date"])

    services.approve_run(ready(run), by=reviewer)

    run.refresh_from_db()
    # What the embargo actually withholds is the *coupling*, not the link: ``approve_run`` skips
    # ``record_compatibility`` and ``_apply_attestation`` while embargoed, so no release row
    # exists yet and there is nothing for the timing gate to sit on. The link itself is set at
    # ingest and stays. (My first version of this asserted ``listing_system is None``, which is
    # not what the embargo does.)
    assert run.listing_system is not None, "the link is from ingest, not from approval"
    assert not run.listing_system.versions.exists(), "the embargo held the certification back"
    assert run.available_from_minor == 3, "and the timing gate survived it"


def test_the_embargo_lifting_leaves_the_timing_gate_in_place(submitter, reviewer):
    """Each on its own schedule. The date arriving publishes the listing; the minor shipping is a
    separate event that has not happened."""
    dell, _ = Vendor.objects.get_or_create(name="Dell Inc.", defaults={"published": True})
    System.objects.create(vendor=dell, name="PowerEdge R760")
    run = _run(submitter, kitten=True, version_id="10.0")
    run.available_from_minor = 3
    run.pre_release = True
    run.publish_requested_date = dt.date(2026, 1, 1)
    run.save(update_fields=["available_from_minor", "pre_release",
                            "publish_requested_date"])
    services.approve_run(ready(run), by=reviewer)

    services.publish_due_runs(today=dt.date(2026, 1, 2))

    run.refresh_from_db()
    assert run.listing_system is not None, "the embargo lifted"
    version = run.listing_system.versions.get(release__major=10)
    assert version.pending_minor == 3, "the timing gate did not lift with it"


# --- the CLI sets it too ----------------------------------------------------------
#
# Reported: "make sure this value can be set from the CLI, but also reviewed/set/overridden from
# the GUI as both the submitter and reviewer." The GUI halves are above; these are the wire.


def _bundle(*, kitten=True, version_id="10.0", **run_meta):
    report = f.make_report(
        run_types=["validate"], version_id=version_id,
        results=[f.validate_result("validate.cpu.functional")],
    )
    if kitten:
        report["environment"]["os"]["pretty_name"] = KITTEN
    report["run"].update(run_meta)
    return f.as_upload(f.build_bundle(report))


def test_the_run_metadata_carries_it(submitter):
    """``--support-from-minor 3`` at ``alma-cert run`` time rides inside the report."""
    run = ingest.ingest_bundle(
        submitter=submitter, source="api", bundle_file=_bundle(support_from_minor=3),
    )

    assert run.available_from_minor == 3


def test_the_submit_field_overrides_the_metadata(submitter):
    """So ``alma-cert submit --support-from-minor`` can correct a run started without it. Same
    arrangement as ``--pre-release`` and ``--publish-after``."""
    run = ingest.ingest_bundle(
        submitter=submitter, source="api", bundle_file=_bundle(support_from_minor=3),
        support_from_minor=1,
    )

    assert run.available_from_minor == 1


def test_it_is_ignored_on_a_shipped_release(submitter):
    """A flag typed on the wrong run must not put a disclaimer on a claim that run just proved.
    A reviewer can still set one by hand where it is genuinely needed."""
    run = ingest.ingest_bundle(
        submitter=submitter, source="api",
        bundle_file=_bundle(kitten=False, support_from_minor=3),
    )

    assert run.available_from_minor is None


@pytest.mark.parametrize("value", ["", "banana", -1, None])
def test_junk_is_dropped_rather_than_refused(submitter, value):
    """Losing a whole run over a courtesy note to the reader would be the wrong trade."""
    run = ingest.ingest_bundle(
        submitter=submitter, source="api", bundle_file=_bundle(support_from_minor=value),
    )

    assert run.available_from_minor is None


def test_the_api_accepts_the_field(submitter):
    """The path the CLI actually posts through."""
    from rest_framework.test import APIClient

    from lumina.accounts.models import ApiToken

    _, raw = ApiToken.issue(
        user=submitter, name="cli", scopes=[ApiToken.SCOPE_SUBMIT],
    )
    api = APIClient()
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")

    resp = api.post(
        "/api/v1/results/",
        {"bundle": _bundle(), "support_from_minor": "3"},
        format="multipart",
    )

    assert resp.status_code in (200, 201), resp.data
    assert TestRun.objects.get(uuid=resp.data["uuid"]).available_from_minor == 3


# --- a hold with no date is a real hold -------------------------------------------
#
# Reported: "the embargo date (or permanent hold if no date) need to be settable by the submitter
# and reviewer in the GUI as well."
#
# The "permanent hold" half was not true when that was written. ``approve_run`` required a
# *future* date, so ticking "unreleased hardware" and leaving the date blank published everything
# at once - and the submitter form's help text, added earlier the same day, promised the opposite.


def test_a_tick_with_no_date_withholds_everything(submitter, reviewer):
    dell, _ = Vendor.objects.get_or_create(name="Dell Inc.", defaults={"published": True})
    System.objects.create(vendor=dell, name="PowerEdge R760")
    run = _run(submitter, version_id="10.0")
    run.pre_release = True
    run.save(update_fields=["pre_release"])

    services.approve_run(ready(run), by=reviewer)

    run.refresh_from_db()
    assert run.published_at is None, "a dateless hold published immediately"
    assert run.is_embargoed is True
    assert not run.listing_system.versions.exists(), "nothing certified while held"


def test_a_date_already_past_publishes_at_once(submitter, reviewer):
    """It has arrived. Only a future date, or no date at all, holds anything back."""
    dell, _ = Vendor.objects.get_or_create(name="Dell Inc.", defaults={"published": True})
    System.objects.create(vendor=dell, name="PowerEdge R760")
    run = _run(submitter, version_id="10.0")
    run.pre_release = True
    run.publish_requested_date = dt.date(2020, 1, 1)
    run.save(update_fields=["pre_release", "publish_requested_date"])

    services.approve_run(ready(run), by=reviewer)

    run.refresh_from_db()
    assert run.published_at is not None


def test_nothing_scheduled_can_end_a_dateless_hold(submitter, reviewer):
    """``publish_due_runs`` filters on a date, so by construction it cannot. That is why a
    reviewer's control is the release path rather than a convenience."""
    dell, _ = Vendor.objects.get_or_create(name="Dell Inc.", defaults={"published": True})
    System.objects.create(vendor=dell, name="PowerEdge R760")
    run = _run(submitter, version_id="10.0")
    run.pre_release = True
    run.save(update_fields=["pre_release"])
    services.approve_run(ready(run), by=reviewer)

    services.publish_due_runs(today=dt.date(2099, 1, 1))

    run.refresh_from_db()
    assert run.published_at is None


def test_a_reviewer_clearing_the_tick_releases_it(client, submitter, reviewer):
    """The manual lift, and the reason ``assign_listing`` publishes rather than only recording:
    a hold with no date waits for a person, so the person's edit has to be the event."""
    dell, _ = Vendor.objects.get_or_create(name="Dell Inc.", defaults={"published": True})
    System.objects.create(vendor=dell, name="PowerEdge R760")
    run = _run(submitter, version_id="10.0")
    run.pre_release = True
    run.save(update_fields=["pre_release"])
    services.approve_run(ready(run), by=reviewer)
    run.refresh_from_db()
    assert run.published_at is None, "the premise"
    client.force_login(reviewer)

    client.post(reverse("review:run_assign_listing", args=[run.pk]), {
        "system": run.listing_system_id,
    })

    run.refresh_from_db()
    assert run.published_at is not None
    assert run.listing_system.versions.exists(), "and the certification landed"


def test_a_reviewer_can_impose_a_hold_too(client, submitter, reviewer):
    run = _run(submitter, version_id="10.0")
    client.force_login(reviewer)

    client.post(reverse("review:run_assign_listing", args=[run.pk]), {
        "pre_release": "on",
    })

    run.refresh_from_db()
    assert run.pre_release is True
    assert run.publish_requested_date is None


def test_the_reviewer_form_shows_what_is_set(submitter):
    from lumina.results.forms import RunListingAssignForm

    run = _run(submitter)
    run.pre_release = True
    run.publish_requested_date = dt.date(2026, 10, 1)
    run.save(update_fields=["pre_release", "publish_requested_date"])

    form = RunListingAssignForm(
        run=run,
        initial={"pre_release": run.pre_release,
                 "publish_requested_date": run.publish_requested_date},
    )

    assert form["pre_release"].value() is True
    assert form["publish_requested_date"].value() == dt.date(2026, 10, 1)


# --- a major with no stable release at all ----------------------------------------
#
# Reported: "there will be a time period where almalinux kitten for a major exists, but there is
# no stable version of almalinux for that major at all. For example in the upcoming 11 cycle,
# kitten will likely exist for 6 months to a year before a stable version exists. We need a way
# to set that no major has been released yet, or something less than 0 I guess."
#
# ``latest_minor`` empty is that state. It is not 0, which means "x.0 has shipped", and it is not
# a sentinel below zero - the field simply has no value until a release exists.


@pytest.fixture
def eleven():
    """AlmaLinux 11: Kitten tracks it, nothing has shipped."""
    release, _ = AlmaLinuxRelease.objects.get_or_create(
        major=11, defaults={"supported": True},
    )
    release.latest_minor = None
    release.save(update_fields=["latest_minor"])
    return release


def test_an_unreleased_major_is_distinguishable_from_x_zero(eleven):
    """The distinction the field could not previously express."""
    ten = AlmaLinuxRelease.objects.get(major=10)

    assert ten.latest_minor == 0 and ten.is_released is True
    assert eleven.latest_minor is None and eleven.is_released is False


def test_nothing_is_live_on_an_unreleased_major(eleven):
    assert eleven.minor_is_live(None) is False
    assert eleven.minor_is_live(0) is False
    assert eleven.minor_is_live(3) is False


def test_a_kitten_run_on_it_certifies_and_publishes(submitter, reviewer, eleven):
    """Same mechanism as usual: the testing runs, the major is certified, the entry publishes
    with a disclaimer."""
    dell, _ = Vendor.objects.get_or_create(name="Dell Inc.", defaults={"published": True})
    System.objects.create(vendor=dell, name="PowerEdge R760")
    run = _run(submitter, kitten=True, version_id="11.0")
    services.approve_run(ready(run), by=reviewer)

    run.refresh_from_db()
    version = run.listing_system.versions.get(release__major=11)
    assert run.listing_system.published is True
    assert version.validation_level == "community", "the major is certified"
    assert version.awaiting_major_release is True


def test_the_disclaimer_says_the_major_is_not_out(submitter, reviewer, eleven):
    """With no minor named, which is the ordinary case here: a submitter validating on Kitten 11
    often cannot know which minor carries their patch, and a blank box would otherwise publish a
    flat "AlmaLinux 11" claim while AlmaLinux 11 does not exist."""
    dell, _ = Vendor.objects.get_or_create(name="Dell Inc.", defaults={"published": True})
    System.objects.create(vendor=dell, name="PowerEdge R760")
    run = _run(submitter, kitten=True, version_id="11.0")
    services.approve_run(ready(run), by=reviewer)

    run.refresh_from_db()
    version = run.listing_system.versions.get(release__major=11)

    assert version.disclaimer == (
        "AlmaLinux 11 support confirmed using AlmaLinux Kitten. "
        "AlmaLinux 11 has not been released yet."
    )


def test_both_facts_appear_when_a_minor_is_named(submitter, reviewer, eleven):
    dell, _ = Vendor.objects.get_or_create(name="Dell Inc.", defaults={"published": True})
    System.objects.create(vendor=dell, name="PowerEdge R760")
    run = _run(submitter, kitten=True, version_id="11.0")
    run.available_from_minor = 2
    run.save(update_fields=["available_from_minor"])
    services.approve_run(ready(run), by=reviewer)

    run.refresh_from_db()
    version = run.listing_system.versions.get(release__major=11)

    assert version.disclaimer == (
        "AlmaLinux 11 support confirmed using AlmaLinux Kitten. "
        "AlmaLinux 11 has not been released yet; the hardware enablement lands in "
        "AlmaLinux 11.2."
    )


def test_the_first_release_lifts_it(submitter, reviewer, eleven):
    """One admin edit, the same one as always: record what has shipped."""
    dell, _ = Vendor.objects.get_or_create(name="Dell Inc.", defaults={"published": True})
    System.objects.create(vendor=dell, name="PowerEdge R760")
    run = _run(submitter, kitten=True, version_id="11.0")
    services.approve_run(ready(run), by=reviewer)
    run.refresh_from_db()
    version = run.listing_system.versions.get(release__major=11)
    assert version.disclaimer, "the premise"

    AlmaLinuxRelease.objects.filter(major=11).update(latest_minor=0)

    version.refresh_from_db()
    assert version.disclaimer == ""
    assert version.awaiting_major_release is False


# --- every gate is per major ------------------------------------------------------
#
# Reported: "this is of course all per-major. Hardware can be supported in a previous major but
# not yet in a new major, or the opposite, so all of this has to be tied to majors."


def test_one_listing_can_be_live_on_one_major_and_gated_on_another(
    submitter, reviewer, eleven,
):
    """The gate lives on the ``ListingVersion`` row, which is per (listing, major) - so a machine
    certified on 9 today and proved on Kitten 11 says both things at once, each on its own row."""
    dell, _ = Vendor.objects.get_or_create(name="Dell Inc.", defaults={"published": True})
    listing = System.objects.create(vendor=dell, name="PowerEdge R760")

    stable = _run(submitter, version_id="9.6")
    services.approve_run(ready(stable), by=reviewer)
    kitten = _run(submitter, kitten=True, version_id="11.0")
    kitten.listing_system = listing
    kitten.available_from_minor = 2
    kitten.save(update_fields=["listing_system", "available_from_minor"])
    services.approve_run(ready(kitten), by=reviewer)

    nine = listing.versions.get(release__major=9)
    eleven_row = listing.versions.get(release__major=11)

    assert nine.disclaimer == "", "a shipped release carries no note"
    assert eleven_row.pending_minor == 2
    assert "has not been released yet" in eleven_row.disclaimer
    # And the reverse direction is possible too: the badge is the highest across majors, so the
    # listing reads certified while one of its majors is still waiting.
    assert listing.validation_level == "community"


def test_lifting_one_major_leaves_the_other_alone(submitter, reviewer, eleven):
    """The opposite direction of the same rule: 11 shipping says nothing about 12."""
    twelve, _ = AlmaLinuxRelease.objects.get_or_create(
        major=12, defaults={"supported": True},
    )
    twelve.latest_minor = None
    twelve.save(update_fields=["latest_minor"])
    dell, _ = Vendor.objects.get_or_create(name="Dell Inc.", defaults={"published": True})
    listing = System.objects.create(vendor=dell, name="PowerEdge R760")
    from lumina.hardware.models import ListingVersion

    for release in (eleven, twelve):
        ListingVersion.objects.create(
            listing_system=listing, release=release,
            source=ListingVersion.SOURCE_RUN,
        )

    AlmaLinuxRelease.objects.filter(major=11).update(latest_minor=0)

    assert listing.versions.get(release__major=11).disclaimer == ""
    assert listing.versions.get(release__major=12).disclaimer != ""


# --- the reviewer has to be able to find them -------------------------------------
#
# Reported as a question: "when reviewing run 3, I don't see a way to gate it as embargoed, but is
# that because it's already a live system listing that we're adding to?"
#
# It was not. The controls were on the page and inside the collapsed block labelled "Attest a
# different listing", which is a different decision entirely - so a reviewer looking for the
# embargo could not find it, and the absence read as deliberate.


def _collapsed(body: str, needle: str) -> bool:
    """Whether ``needle`` sits inside a CSS-collapsed reveal block.

    A field hidden behind a disclosure is present in the markup and invisible to a reader, which
    no string assertion would have caught: every test of this page passed while the embargo
    controls were unreachable.
    """
    at = body.index(needle)
    before = body[:at]
    # The last reveal block opened before the field, versus the last one closed.
    return before.rfind("reveal-fields") > before.rfind("</div></div>")


def test_the_gates_are_shown_outright(client, submitter, reviewer):
    run = _run(submitter)
    client.force_login(reviewer)

    body = client.get(reverse("review:run_detail", args=[run.pk])).content.decode()

    assert not _collapsed(body, 'name="pre_release"')
    assert not _collapsed(body, 'name="publish_requested_date"')
    assert "Withhold this run" in body


def test_the_assignment_stays_behind_its_override(client, submitter, reviewer):
    """The half that *is* an override of what the page already states, and should stay put."""
    run = _run(submitter)
    client.force_login(reviewer)

    body = client.get(reverse("review:run_detail", args=[run.pk])).content.decode()

    assert _collapsed(body, 'name="claimed_validation_level"')
    assert _collapsed(body, 'name="machine_kind"')


def test_a_run_against_a_live_listing_can_still_be_withheld(client, submitter, reviewer):
    """The question behind the report: whether an existing listing was the reason. It is not - a
    re-validation of published hardware is withheld the same way anything else is."""
    dell, _ = Vendor.objects.get_or_create(name="Dell Inc.", defaults={"published": True})
    listing = System.objects.create(
        vendor=dell, name="PowerEdge R760", published=True,
    )
    run = _run(submitter)
    assert run.listing_system == listing, "the premise: adding to a live listing"
    client.force_login(reviewer)

    client.post(reverse("review:run_assign_listing", args=[run.pk]), {
        "system": listing.pk, "pre_release": "on",
    })

    run.refresh_from_db()
    assert run.pre_release is True
    assert run.listing_system == listing, "and the assignment survived the same post"


def test_saving_the_gates_does_not_unlink_the_listings(client, submitter, reviewer):
    """One form and one button, because the endpoint treats a blank box as a decision to clear.
    Two forms would have meant saving the embargo silently unassigned the run."""
    dell, _ = Vendor.objects.get_or_create(name="Dell Inc.", defaults={"published": True})
    listing = System.objects.create(vendor=dell, name="PowerEdge R760", published=True)
    run = _run(submitter)
    client.force_login(reviewer)
    body = client.get(reverse("review:run_detail", args=[run.pk])).content.decode()

    # One <form> around both sections, so the browser posts the assignment with the gates.
    start = body.index('name="pre_release"')
    assert "</form>" not in body[start:body.index('name="claimed_validation_level"')], (
        "the gates and the assignment are in separate forms, so saving one clears the other"
    )
    assert run.listing_system == listing


# --- a run's gate never re-gates a listing that is already public -------------------
#
# Asked: "what happens if there is an existing listing that a new run is being tied to, and the
# publication is marked as unreleased? Will it gate the whole listing, or does the listing get
# published if any of the runs are public? We don't want to erroneously allow a re-run with
# gating to re-gate an already-public system listing."
#
# Measured, both gates:
#
#   after a public run           listing.published=True  gate=None  disclaimer=''
#   + an embargoed run           listing.published=True  attestations=1  run.published_at=None
#   + a gated Kitten run         gate=None  pending=None  disclaimer=''
#
# Neither touches the listing. An embargo scopes to its own run, and a timing gate only ever
# loosens - so a re-run cannot put a disclaimer back on a claim that is already live.


@pytest.fixture
def live_listing(submitter, reviewer):
    """A published listing with one public run behind it."""
    dell, _ = Vendor.objects.get_or_create(name="Dell Inc.", defaults={"published": True})
    listing = System.objects.create(vendor=dell, name="PowerEdge R760")
    first = _run(submitter, version_id="10.0")
    services.approve_run(ready(first), by=reviewer)
    listing.refresh_from_db()
    assert listing.published is True, "the premise"
    assert listing.versions.get(release__major=10).disclaimer == ""
    return listing


def _second_run(submitter, listing, **fields):
    run = _run(submitter, kitten=fields.pop("kitten", False), version_id="10.0")
    run.listing_system = listing
    for name, value in fields.items():
        setattr(run, name, value)
    run.save(update_fields=["listing_system", *fields])
    return run


def test_an_embargoed_run_does_not_unpublish_the_listing(submitter, reviewer, live_listing):
    run = _second_run(
        submitter, live_listing,
        pre_release=True, publish_requested_date=dt.date(2099, 1, 1),
    )

    services.approve_run(ready(run), by=reviewer)

    live_listing.refresh_from_db()
    assert live_listing.published is True
    assert live_listing.versions.get(release__major=10).disclaimer == ""


def test_an_embargoed_run_contributes_nothing_until_it_lifts(
    submitter, reviewer, live_listing,
):
    """The embargo scopes to the run. Its evidence is withheld, which is the point; what it must
    not do is take anything away from what is already public."""
    before = live_listing.attestation_count
    run = _second_run(
        submitter, live_listing,
        pre_release=True, publish_requested_date=dt.date(2099, 1, 1),
    )

    services.approve_run(ready(run), by=reviewer)

    live_listing.refresh_from_db()
    run.refresh_from_db()
    assert run.published_at is None, "the run is held"
    assert live_listing.attestation_count == before, "and adds nothing yet"
    assert run not in TestRun.objects.public(), "so it is not shown as evidence either"


def test_a_gated_kitten_rerun_cannot_re_gate_a_live_claim(submitter, reviewer, live_listing):
    """The case named in the report. A gate only ever loosens, so a later Kitten run naming a
    minor cannot put a disclaimer on a major that a shipped release already proved."""
    run = _second_run(submitter, live_listing, kitten=True, available_from_minor=3)

    services.approve_run(ready(run), by=reviewer)

    version = live_listing.versions.get(release__major=10)
    assert version.available_from_minor is None
    assert version.pending_minor is None
    assert version.disclaimer == ""


def test_a_gated_rerun_still_gates_a_major_the_listing_did_not_have(
    submitter, reviewer, live_listing, eleven,
):
    """The other half: nothing about protecting a live major protects an unproved one. A Kitten
    run on 11 gates the 11 row it creates and leaves 10 alone."""
    run = _run(submitter, kitten=True, version_id="11.0")
    run.listing_system = live_listing
    run.available_from_minor = 2
    run.save(update_fields=["listing_system", "available_from_minor"])

    services.approve_run(ready(run), by=reviewer)

    assert live_listing.versions.get(release__major=10).disclaimer == ""
    assert live_listing.versions.get(release__major=11).pending_minor == 2


# --- and the reviewer's assignment path respects the embargo too --------------------
#
# It did not. ``assign_listing`` applies the certification coupling for an already-approved run,
# because approval may have gone by with no listing to apply it to - and it checked only that the
# run was approved. So touching the assignment on an approved *held* run published it at once.
#
# Much more reachable since the withhold controls started posting to that endpoint: a reviewer
# imposing an embargo on an approved run would have published its certification in the same
# request.


def _held_on_a_new_major(submitter, reviewer, listing):
    """An approved-but-held run on a major the listing does not carry yet.

    The choice of major is what makes a leak visible. My first attempt used the same major as
    the public run and asserted on ``run.published_at`` and the attestation count: neither moves.
    ``published_at`` is not what this path touches, and ``_attest_one`` dedups per (version,
    person), so a second run by the same submitter on the same major records nothing either way.
    Both assertions passed with the guard deliberately removed.

    A major the listing has never claimed cannot be deduped against anything, so a new
    ``ListingVersion`` row appearing is unambiguous evidence that the coupling ran.
    """
    run = _run(submitter, version_id="9.6")
    run.listing_system = listing
    run.pre_release = True
    run.save(update_fields=["listing_system", "pre_release"])
    services.approve_run(ready(run), by=reviewer)
    run.refresh_from_db()
    assert run.published_at is None, "the premise: approved and held"
    assert not listing.versions.filter(release__major=9).exists(), (
        "the premise: nothing certified for 9 yet"
    )
    return run


def test_editing_the_assignment_does_not_certify_a_held_run(
    submitter, reviewer, live_listing,
):
    run = _held_on_a_new_major(submitter, reviewer, live_listing)

    services.assign_listing(run, system=live_listing, by=reviewer)

    assert not live_listing.versions.filter(release__major=9).exists(), (
        "the assignment path certified a run it was withholding"
    )
    run.refresh_from_db()
    assert run.published_at is None


def test_imposing_an_embargo_through_the_review_form_does_not_certify(
    client, submitter, reviewer, live_listing,
):
    """The route my own change opened: the withhold controls post to this endpoint, so a reviewer
    imposing a hold on an approved run passed through the coupling on the way."""
    run = _held_on_a_new_major(submitter, reviewer, live_listing)
    client.force_login(reviewer)

    client.post(reverse("review:run_assign_listing", args=[run.pk]), {
        "system": live_listing.pk, "pre_release": "on",
    })

    run.refresh_from_db()
    assert run.pre_release is True
    assert run.published_at is None
    assert not live_listing.versions.filter(release__major=9).exists()


def test_clearing_the_hold_through_the_form_does_certify_it(
    client, submitter, reviewer, live_listing,
):
    """The opposite edit, and the reason the guard is on the embargo rather than on the endpoint:
    a reviewer lifting a dateless hold is exactly when the coupling should be applied."""
    run = _held_on_a_new_major(submitter, reviewer, live_listing)
    client.force_login(reviewer)

    client.post(reverse("review:run_assign_listing", args=[run.pk]), {
        "system": live_listing.pk,
    })

    run.refresh_from_db()
    assert run.published_at is not None
    assert live_listing.versions.filter(release__major=9).exists()


def test_a_published_run_still_couples_on_reassignment(submitter, reviewer):
    """No regression in what the path is for: a reviewer assigning a listing to an approved run
    that was never held has to apply the certification, since approval had nothing to apply."""
    dell, _ = Vendor.objects.get_or_create(name="Dell Inc.", defaults={"published": True})
    run = _run(submitter, version_id="10.0")
    services.approve_run(ready(run), by=reviewer)
    other = System.objects.create(vendor=dell, name="PowerEdge R770")

    services.assign_listing(run, system=other, by=reviewer)

    assert other.versions.filter(release__major=10).exists()
    other.refresh_from_db()
    assert other.attestation_count == 1
