"""Saying the machine a run was matched to is not this machine.

Identity matching is a heuristic over firmware strings and it is right almost always, which is
why the identity fields are locked once a run is linked: the listing belongs to somebody else
and restating it is not the submitter's business.

But two different machines really can report the same vendor and model - a rebadge, a
barebones chassis sold by several integrators, a DMI table nobody filled in - and until now a
run auto-linked at ingest was stuck attesting somebody else's listing with no way to say so.

Gated as its own deliberate act rather than by leaving the fields editable, because editable by
default would invite rewriting a listing that is simply correct.

**The override is a field of the proposal form, not an endpoint of its own.** It was an
endpoint, and the button had to sit inside the proposal form to be anywhere useful - which made
it a nested ``<form>``. Browsers ignore the inner one and submit the outer, so pressing it
saved the proposal with the identity fields absent: reported as "redirects to the run overview,
the fields are blanked out, and the button is still there", which is three symptoms of that one
cause. Now the checkbox that reveals the fields is the value that posts, so the two cannot
disagree, and ``test_the_form_holds_no_nested_form`` is what keeps it that way.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth.models import Group, User
from django.urls import reverse

from lumina.core.certification import ValidationLevel
from lumina.hardware.models import System
from lumina.releases.models import AlmaLinuxRelease
from lumina.results import ingest, services
from lumina.results.forms import RunListingProposalForm
from lumina.results.models import TestRun
from lumina.results.tests import factories as f
from lumina.results.tests.helpers import release
from lumina.vendors.models import Vendor

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def alma_nine():
    AlmaLinuxRelease.objects.get_or_create(major=9, defaults={"supported": True})


@pytest.fixture
def submitter():
    return User.objects.create_user("disputer", password="pw")


@pytest.fixture
def reviewer():
    user = User.objects.create_user("dispute-rev")
    group, _ = Group.objects.get_or_create(name="reviewer")
    user.groups.add(group)
    return user


def _run(submitter):
    return ingest.ingest_bundle(
        submitter=submitter, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"],
            results=[f.validate_result("validate.cpu.functional")],
        ))),
    )


@pytest.fixture
def matched(submitter):
    """A run auto-linked at ingest to somebody else's listing."""
    dell = Vendor.objects.create(name="Dell Inc.", published=True)
    listing = System.objects.create(vendor=dell, name="PowerEdge R760")
    run = _run(submitter)
    assert run.listing_system == listing, "the fixture must start matched"
    return run, listing


def _form_url(run):
    return reverse("results:propose_listing", args=[run.uuid])


def _post(client, run, **extra):
    return client.post(_form_url(run), extra)


def _dispute(client, run, name="Totally Other Box", vendor="Dell Inc."):
    """What the browser sends: the ticked override and the machine's real identity, in one
    request, because the fields are revealed client-side."""
    return _post(client, run, identity_disputed="1", vendor_name=vendor, name=name)


def _undo(client, run):
    return _post(client, run, undo_identity_dispute="1")


# --- the control ------------------------------------------------------------------


def test_the_form_opens_for_a_bystander(client, matched, submitter):
    """The premise, and it used to be a 302.

    The submitter who needs the override is by definition a bystander to the listing they were
    matched to, so gating the page on speaking for that listing's vendor made the control
    unreachable by exactly the people it was built for.
    """
    run, _ = matched
    client.force_login(submitter)

    assert client.get(_form_url(run)).status_code == 200


def test_the_override_is_offered_on_the_form(client, matched, submitter):
    run, _ = matched
    client.force_login(submitter)

    body = client.get(_form_url(run)).content.decode()

    assert "This is not that machine" in body
    assert 'name="identity_disputed"' in body


def test_the_run_page_points_at_it(client, matched, submitter):
    """That page is where somebody notices the match is wrong; the control itself lives with
    the fields it reveals."""
    run, _ = matched
    client.force_login(submitter)

    body = client.get(run.get_absolute_url()).content.decode()

    assert "Not this machine?" in body
    assert _form_url(run) in body


def test_the_fields_are_already_there_to_reveal(client, matched, submitter):
    """The reported requirement: pressing the button makes the fields appear, with no round
    trip. That only works if the server rendered them, so this asserts they are in the body of
    the first GET - and inside the block the toggle controls, not loose on the page."""
    run, _ = matched
    client.force_login(submitter)

    body = client.get(_form_url(run)).content.decode()

    # By position rather than by slicing on whitespace: each field must appear once, after the
    # block that the toggle reveals and before the next card. A field rendered loose on the page
    # would be visible to a reader who never pressed anything.
    assert body.count('name="vendor_name"') == 1
    assert body.index('name="vendor_name"') > body.index('class="identity-override')
    assert body.index('name="vendor_name"') < body.index("AlmaLinux compatibility")


def test_the_form_holds_no_nested_form(client, matched, submitter):
    """The bug itself. A ``<form>`` inside a ``<form>`` is dropped by every browser, so a
    button in the inner one submits the outer: the proposal saved with the identity fields
    absent, which blanked them, and nothing was ever disputed."""
    run, _ = matched
    client.force_login(submitter)
    body = client.get(_form_url(run)).content.decode()

    # By id, not by the first ``<form method="post"``: that found the nav's sign-out form,
    # and this guard spent its first run asserting that the sign-out button contains no nested
    # form. It passed with a nested form deliberately added to this page.
    start = body.index('id="listing-details"')
    inner = body[start:body.index("</form>", start)]

    assert "<form" not in inner


def test_the_reveal_is_wired_to_the_stylesheet(client, matched, submitter):
    """The reveal is pure CSS off the checkbox's own ``:checked`` state, which is what makes the
    control that opens the fields the same value that posts. That also makes the class names
    load-bearing: rename one in the template and the block silently renders open for everybody,
    or never opens at all, and no other test would notice.

    One rule set, two users - this override and the per-component one - so a check here covers
    both.
    """
    run, _ = matched
    client.force_login(submitter)
    body = client.get(_form_url(run)).content.decode()
    # In the *shared* sheet, not the public one: the same rules gate the per-component override,
    # which the reviewer's page renders under `base_admin.html`. Living only in
    # `lumina-public.css` meant those fields were permanently open there. See
    # `core/tests/test_shared_stylesheets.py`.
    css = (Path(settings.BASE_DIR) / "static" / "css" / "lumina-shared.css").read_text()

    for name in ("reveal-toggle", "reveal-fields", "reveal-when-open", "reveal-when-closed"):
        assert name in body, f"{name} is not in the rendered form"
        assert name in css, f"{name} is in the template but has no rule"
    # Collapsed by default, opened by the checkbox. Without the first rule the fields are
    # simply always visible, which is the state this whole control exists to gate.
    rules = " ".join(css.split())
    assert ".reveal-fields, .reveal-toggle:checked" in rules
    assert ".reveal-toggle:checked ~ .reveal-fields { display: block" in rules


def test_it_is_not_offered_when_creating_a_listing(client, submitter):
    """Nothing to disown. The form is already asking them to describe the machine."""
    client.force_login(submitter)
    run = _run(submitter)

    body = client.get(_form_url(run)).content.decode()

    assert "This is not that machine" not in body
    assert 'name="vendor_name"' in body, "the identity fields are theirs outright here"


def test_somebody_elses_run_is_a_404(client, matched):
    run, _ = matched
    client.force_login(User.objects.create_user("nosy", password="pw"))

    assert _dispute(client, run).status_code == 404


def test_a_submitted_run_cannot_be_disputed(client, matched, submitter):
    """Once it is with a reviewer, unlinking it under them would change what they are
    looking at."""
    run, listing = matched
    services.submit_for_review(TestRun.objects.get(pk=run.pk), by=submitter)
    client.force_login(submitter)

    _dispute(client, run)

    run.refresh_from_db()
    assert run.identity_disputed is False
    assert run.listing_system == listing


# --- what it does -----------------------------------------------------------------


def test_disputing_unlinks_the_run(client, matched, submitter):
    """Clearing the link matters as much as the flag: approval reuses a linked listing without
    consulting anything else."""
    run, _ = matched
    client.force_login(submitter)

    _dispute(client, run)

    run.refresh_from_db()
    assert run.identity_disputed is True
    assert run.listing_system is None


def test_the_identity_arrives_in_the_same_save(client, matched, submitter):
    """One request carries both, so there is no window where the fields are editable but the
    flag is not yet set - and nothing the submitter typed is thrown away on the way through.

    This is the shape the old endpoint could not have: it saved the flag, redirected, and left
    the submitter to fill the fields in a second request.
    """
    run, _ = matched
    client.force_login(submitter)

    _dispute(client, run, vendor="Supermicro", name="Whitebox 1U")

    run.refresh_from_db()
    assert run.identity_disputed is True
    assert run.listing_proposal["vendor_name"] == "Supermicro"
    assert run.listing_proposal["name"] == "Whitebox 1U"


def test_the_identity_fields_unlock(client, matched, submitter):
    """The point of the override. Before it, a matched run showed the fields to nobody but the
    listing's own vendor, so the person holding a misidentified machine could not describe it.

    Locked, not absent, since the override has to be able to reveal them - so what changes is
    ``identity_locked``, and the fields go from discarded to honoured.
    """
    run, _ = matched
    client.force_login(submitter)
    before = RunListingProposalForm(run=run, user=submitter, subject="system")
    assert before.identity_locked is True

    _dispute(client, run)

    after = RunListingProposalForm(
        run=TestRun.objects.get(pk=run.pk), user=submitter, subject="system",
    )
    assert after.identity_locked is False
    assert after.fields["name"].required is True


def test_a_name_is_required_to_dispute(client, matched, submitter):
    """A new listing needs one. The fields are optional while locked - a collapsed block nobody
    opened must not raise errors - so the requirement is stated for the disputing path only."""
    run, _ = matched
    client.force_login(submitter)

    resp = _post(client, run, identity_disputed="1", vendor_name="Dell Inc.", name="")

    assert resp.status_code == 200
    assert "so it can be listed on its own" in resp.content.decode()
    run.refresh_from_db()
    assert run.identity_disputed is False


def test_approval_creates_a_new_listing(client, matched, submitter, reviewer):
    run, original = matched
    client.force_login(submitter)
    _dispute(client, run, name="PowerEdge R760 Variant")

    services.approve_run(release(TestRun.objects.get(pk=run.pk)), by=reviewer)

    run.refresh_from_db()
    assert run.listing_system is not None
    assert run.listing_system.pk != original.pk
    assert run.listing_system.name == "PowerEdge R760 Variant"


def test_the_reported_identity_does_not_drag_the_old_listing_back(
    client, matched, submitter, reviewer,
):
    """The subtle half. ``resolve_reported_system`` matches on the *reported* strings, which are
    exactly what was wrong, so leaving it in place re-attached the disputed listing however the
    submitter renamed the machine."""
    run, original = matched
    client.force_login(submitter)
    # Deliberately renamed to something that still reports the same DMI strings.
    _dispute(client, run, name="Totally Other Box")

    services.approve_run(release(TestRun.objects.get(pk=run.pk)), by=reviewer)

    run.refresh_from_db()
    assert run.listing_system.pk != original.pk


def test_the_original_listing_is_left_alone(client, matched, submitter, reviewer):
    """Disowning a match must not touch the listing that was matched."""
    run, original = matched
    client.force_login(submitter)
    _dispute(client, run, name="Other Box")

    services.approve_run(release(TestRun.objects.get(pk=run.pk)), by=reviewer)

    original.refresh_from_db()
    assert original.name == "PowerEdge R760"
    assert original.attestation_count == 0
    assert original.published is False


# --- taking it back ---------------------------------------------------------------


def test_it_can_be_taken_back(client, matched, submitter):
    """A misclick has to be recoverable, and undoing it restores the match: with the flag
    cleared, approval resolves the reported identity again."""
    run, original = matched
    client.force_login(submitter)
    _dispute(client, run)

    _undo(client, run)

    run.refresh_from_db()
    assert run.identity_disputed is False
    assert services.existing_listing_for(run) == original


def test_the_undo_is_offered_once_disputed(client, matched, submitter):
    run, _ = matched
    client.force_login(submitter)
    _dispute(client, run)

    body = client.get(_form_url(run)).content.decode()

    assert "Actually, it is that machine" in body


def test_the_undo_does_not_need_a_valid_form(client, matched, submitter):
    """Handled before validation. A disputed run has ``name`` required again, so an undo that
    went through the form would be refused for the very field the submitter is abandoning."""
    run, _ = matched
    client.force_login(submitter)
    _dispute(client, run)

    resp = client.post(_form_url(run), {"undo_identity_dispute": "1", "name": ""})

    assert resp.status_code == 302
    run.refresh_from_db()
    assert run.identity_disputed is False


def test_a_later_save_does_not_silently_undo_it(client, matched, submitter):
    """Once disputed, the form renders as a plain identity card and the checkbox is not on the
    page at all - so reading a missing checkbox as False would re-link the run on the next
    ordinary save. The flag is set here and cleared only by the undo."""
    run, _ = matched
    client.force_login(submitter)
    _dispute(client, run, name="Whitebox 1U")

    _post(client, run, vendor_name="Supermicro", name="Whitebox 1U")

    run.refresh_from_db()
    assert run.identity_disputed is True


# --- what it is not a way round ---------------------------------------------------


def test_locked_identity_values_are_discarded(client, matched, submitter):
    """The guarantee that used to come from the fields being absent, now that they are present
    but collapsed: a post naming somebody else's listing changes nothing without the override.

    Belt and braces - ``create_listings_from_run`` never applies a proposal's identity to an
    existing listing either - but this is the layer that keeps the stored proposal honest.
    """
    run, listing = matched
    client.force_login(submitter)

    _post(client, run, vendor_name="Nobody", name="Hijacked")

    run.refresh_from_db()
    assert "name" not in run.listing_proposal
    assert "vendor_name" not in run.listing_proposal
    assert run.listing_system == listing


def test_a_disputed_run_is_treated_as_new_hardware_throughout(client, matched, submitter):
    """One flag, every consumer. The identity fields, the attribution list, and approval all key
    off ``existing_listing_for``, so they agree without each needing to know about disputes."""
    run, _ = matched
    client.force_login(submitter)
    _dispute(client, run)
    run = TestRun.objects.get(pk=run.pk)

    assert services.existing_listing_for(run) is None
    assert RunListingProposalForm(
        run=run, user=submitter, subject="system",
    ).identity_locked is False
    # The reported manufacturer survives, and should: disputing says "not that listing", not
    # "not a Dell". A misidentified Dell is usually still a Dell, and a Dell engineer disputing
    # one needs to be able to attribute the run to Dell.
    assert [v.name for v in services.identity_vendors(run)] == ["Dell Inc."]


def test_the_dispute_is_recorded_in_the_audit_log(client, matched, submitter):
    """A submitter disowning a catalog match is a decision a reviewer may want to see."""
    from lumina.audit.models import AuditLogEntry

    run, _ = matched
    client.force_login(submitter)

    _dispute(client, run)

    entry = AuditLogEntry.objects.filter(action="test_run.identity_disputed").first()
    assert entry is not None
    assert entry.after == {"disputed": True}


def test_a_community_submitter_still_gets_no_vendor_tier_from_it(
    client, matched, submitter, reviewer,
):
    """Disputing is not a way round the trust rules - it changes which listing the evidence is
    about, not what the evidence is worth."""
    run, _ = matched
    client.force_login(submitter)
    _dispute(client, run, name="Some Other Box")
    run.refresh_from_db()
    run.claimed_validation_level = ValidationLevel.VENDOR
    run.save(update_fields=["claimed_validation_level"])

    services.approve_run(release(TestRun.objects.get(pk=run.pk)), by=reviewer)

    run.refresh_from_db()
    assert run.listing_system.validation_level == ValidationLevel.COMMUNITY
