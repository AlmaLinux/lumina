"""Editing a listing proposal adds AlmaLinux support; it never retracts it.

``propose_listing`` used to assign ``run.listing_proposal = data``, replacing the
whole blob with whatever the form posted. That silently retracted support claims
two ways:

1. Unticking a release removed a major the submitter had already stated - fine if
   they meant it, but the same thing happened when they came back to fix a typo in
   the description and the boxes rendered differently.
2. A major that stops being ``supported()`` disappears from the form entirely, so
   the next save of *any* field dropped it without anyone touching it.

Both are now impossible: majors union.

A minor floor used to travel with each major, and the merge took the lower of the two
because the lower floor is the broader claim. Hardware certifies per major now, so the
floor is gone and with it the arithmetic - what remains is the union, which was always
the part that mattered here. Legacy ``release_minor_*`` keys are dropped as blobs pass
through the merge.

Everything else still overwrites. A description, a model number, and a CPU family
are corrections, and a form that could not fix them would be worse than one that
loses a checkbox.
"""
from __future__ import annotations

import re

import pytest
from django.contrib.auth.models import Group, User
from django.urls import reverse

from lumina.releases.models import AlmaLinuxRelease
from lumina.results import ingest, services
from lumina.results.tests import factories as f

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def releases():
    for major in (8, 9, 10):
        AlmaLinuxRelease.objects.get_or_create(
            major=major, defaults={"supported": True, "max_minor": 10},
        )


@pytest.fixture
def submitter():
    return User.objects.create_user("proposer", password="pw")


@pytest.fixture
def reviewer():
    user = User.objects.create_user("proposal-reviewer", password="pw")
    group, _ = Group.objects.get_or_create(name="reviewer")
    user.groups.add(group)
    return user


def _run(submitter, version_id="9.6"):
    return ingest.ingest_bundle(
        submitter=submitter, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["collect", "validate"], version_id=version_id,
            results=[f.validate_result("validate.cpu.functional")],
        ))),
    )


# --- the merge rule itself ----------------------------------------------------


def test_a_previously_claimed_major_survives_an_untick():
    merged = services.merge_listing_proposal(
        {"release_8": True, "release_minor_8": 10},
        {"release_9": True, "release_minor_9": 6},
    )

    assert merged["release_8"] is True
    assert merged["release_9"] is True


def test_a_major_missing_from_the_form_entirely_survives():
    """The case nobody could have caused deliberately: a release that stops being
    ``supported()`` is not rendered, so it is absent from the post."""
    merged = services.merge_listing_proposal(
        {"release_8": True, "release_minor_8": 4, "name": "R760"},
        {"name": "PowerEdge R760"},          # a description fix, no release keys
    )

    assert merged["release_8"] is True


def test_legacy_minor_keys_are_dropped():
    """Blobs saved before hardware went majors-only still carry ``release_minor_*``.

    Nothing reads them. Left in place they would keep surfacing in the audit log and on the
    review page for years, so the merge sheds them - which is also what stops a stale
    ``release_minor_9: 0`` being mistaken for a claim on some major.
    """
    merged = services.merge_listing_proposal(
        {"release_9": True, "release_minor_9": 6},
        {"release_9": True, "release_minor_9": 4},
    )

    assert merged == {"release_9": True}


def test_a_major_survives_the_shedding():
    """The tick is the claim, and dropping the minor beside it must not drop the tick."""
    merged = services.merge_listing_proposal(
        {"release_9": True, "release_minor_9": 6}, {"release_minor_9": 4},
    )

    assert merged["release_9"] is True


def test_an_unticked_release_is_still_not_a_claim():
    """It never was, and the reason is unchanged: only a truthy ``release_<major>`` counts.

    This used to guard something subtler - the minor dropdown posted on every save whether
    its box was ticked or not, so reading it unconditionally broadened "9.6+" to "9.0+".
    """
    merged = services.merge_listing_proposal({}, {"release_9": False})

    # The key rides along as posted - the form sends every checkbox - and being falsy it is not
    # a claim: nothing downstream reads it as one.
    assert merged["release_9"] is False
    assert services._claimed_majors(merged) == set()


def test_a_new_major_is_added():
    merged = services.merge_listing_proposal(
        {"release_9": True, "release_minor_9": 6},
        {"release_9": True, "release_minor_9": 6,
         "release_10": True, "release_minor_10": 2},
    )

    assert merged["release_10"] is True


def test_everything_else_still_overwrites():
    """Corrections have to work. Only the release claim is additive."""
    merged = services.merge_listing_proposal(
        {"name": "R760", "description": "old", "cpu_model": "Xeon"},
        {"name": "PowerEdge R760", "description": "new", "cpu_model": "Xeon Gold"},
    )

    assert merged["name"] == "PowerEdge R760"
    assert merged["description"] == "new"
    assert merged["cpu_model"] == "Xeon Gold"


@pytest.mark.parametrize("blob", [None, {}, {"release_x": True},
                                  {"release_9": True, "release_minor_9": "junk"}])
def test_malformed_blobs_do_not_raise(blob):
    services.merge_listing_proposal(blob, {"release_9": True})
    services.merge_listing_proposal({"release_9": True}, blob)


# --- through the actual endpoint ----------------------------------------------


def _speaks_for_the_vendor(user, vendor_name="Dell Inc."):
    """Give ``user`` submit rights at the machine's manufacturer.

    Needed by any test whose second post lands on hardware that is already in the
    catalog: ``propose_listing`` now refuses a re-validation unless the submitter speaks
    for the vendor, because the form's identity fields would otherwise let anyone rewrite
    a manufacturer's own entry. These tests are about the *merge* rule rather than about
    permissions, so they take the role that lets them exercise it.
    """
    from lumina.vendors.models import Vendor, VendorMembership

    vendor, _ = Vendor.objects.get_or_create(
        name=vendor_name, defaults={"verified": True},
    )
    VendorMembership.objects.get_or_create(
        user=user, vendor=vendor,
        defaults={"role": VendorMembership.ROLE_SUBMITTER},
    )
    return vendor


def _post(client, run, **extra):
    data = {
        "vendor_name": "Dell Inc.", "name": "PowerEdge R760",
        "machine_kind": "prebuilt", "cpu_model": "Xeon Gold 6430",
    }
    data.update(extra)
    return client.post(
        reverse("results:propose_listing", args=[run.uuid]), data,
    )


def test_the_endpoint_keeps_a_release_from_an_earlier_save(client, submitter):
    """The reported bug, end to end."""
    run = _run(submitter)
    client.force_login(submitter)
    _post(client, run, release_8="on")
    run.refresh_from_db()
    assert run.listing_proposal["release_8"] is True

    # Second visit: only 9 ticked, 8 left alone.
    _post(client, run, release_9="on")

    run.refresh_from_db()
    assert run.listing_proposal["release_8"] is True, "AlmaLinux 8 was dropped"
    assert run.listing_proposal["release_9"] is True


def test_the_endpoint_still_lets_a_typo_be_fixed(client, submitter):
    """Only the *release* selection accumulates. Everything else is a correction and the
    latest answer wins, which is the whole reason ``merge_listing_proposal`` treats the
    two differently.

    Used to make this point with ``description``, which is no longer on the form: it is a
    property of a listing rather than a statement about a run, and it was silently
    discarded on any re-validation. ``model_number`` is the same kind of correctable
    fact.
    """
    run = _run(submitter)
    client.force_login(submitter)
    _post(client, run, model_number="R760SX")

    _post(client, run, model_number="R760XS")

    run.refresh_from_db()
    assert run.listing_proposal["model_number"] == "R760XS"


def test_the_audit_entry_records_what_was_stored(client, submitter):
    """Not the posted blob, which now differs from it."""
    from lumina.audit.models import AuditLogEntry

    run = _run(submitter)
    client.force_login(submitter)
    _post(client, run, release_8="on")
    _post(client, run, release_9="on")

    entry = AuditLogEntry.objects.filter(
        action="test_run.propose_listing"
    ).order_by("-created_at").first()

    assert entry.after["release_8"] is True, "the log disagrees with the database"


# --- sharing across sibling drafts -------------------------------------------


def test_sharing_details_does_not_drop_a_siblings_release(submitter):
    """"These details were applied to your other runs" must not mean a release
    quietly disappeared from one of them."""
    nine = _run(submitter, version_id="9.6")
    ten = _run(submitter, version_id="10.2")
    ten.listing_proposal = {"release_10": True, "release_minor_10": 2}
    ten.save(update_fields=["listing_proposal"])
    nine.listing_proposal = {"release_9": True, "release_minor_9": 6,
                             "name": "PowerEdge R760"}
    nine.save(update_fields=["listing_proposal"])

    shared = services.share_listing_details(nine)

    assert shared, "no sibling was found to share with"
    ten.refresh_from_db()
    assert ten.listing_proposal["release_10"] is True, "the sibling lost its own major"
    assert ten.listing_proposal["release_9"] is True, "and did not gain the new one"
    assert ten.listing_proposal["name"] == "PowerEdge R760"


# --- a *new* run inherits what the machine already claims ----------------------
#
# The first fix covered re-saving one run's form. It did not cover the reported
# sequence, which is different: set all three majors, then upload a run on 10 and
# find 8 and 9 unticked. That path never went through the merge at all - the
# prefill for a fresh run borrowed only from *draft* siblings, so the moment the
# earlier run was sent for review or approved its selection became invisible, and
# saving the new form made the loss real.
#
# ``claimed_release_ticks`` now reads two sources that do not depend on run status:
# the catalog listing's ``ListingVersion`` rows, and the submitter's other runs of
# the same machine whatever state they are in.


def _post_all_three(client, run):
    return client.post(
        reverse("results:propose_listing", args=[run.uuid]),
        {
            "vendor_name": "Dell Inc.", "name": "PowerEdge R760",
            "machine_kind": "prebuilt", "cpu_model": "Xeon Gold 6430",
            "release_8": "on", "release_minor_8": "10",
            "release_9": "on", "release_minor_9": "6",
            "release_10": "on", "release_minor_10": "0",
        },
    )


def _checked_on_form(client, run):
    body = client.get(
        reverse("results:propose_listing", args=[run.uuid])
    ).content.decode()
    return sorted(re.findall(r'name="(release_\d+)"[^>]*checked', body))


@pytest.mark.parametrize("earlier_state", ["draft", "pending", "approved"])
def test_a_new_run_inherits_every_major_already_claimed(
    client, submitter, reviewer, earlier_state
):
    """The reported bug, in all three states of the earlier run.

    ``pending`` and ``approved`` are the ones that were broken: only ``draft`` was
    ever consulted, so submitting the first run for review was enough to lose the
    selection on the next upload.
    """
    from lumina.results.models import TestRun

    first = _run(submitter, version_id="9.6")
    _speaks_for_the_vendor(submitter)
    client.force_login(submitter)
    _post_all_three(client, first)

    # Re-fetched from the database each time: a stale in-memory run passed to
    # submit_for_review makes ``missing_submission_details`` see an empty proposal
    # and overwrite the saved one, which looks exactly like the bug under test.
    if earlier_state in ("pending", "approved"):
        services.submit_for_review(TestRun.objects.get(pk=first.pk), by=submitter)
    if earlier_state == "approved":
        services.approve_run(TestRun.objects.get(pk=first.pk), by=reviewer)

    later = _run(submitter, version_id="10.2")

    assert _checked_on_form(client, later) == [
        "release_10", "release_8", "release_9",
    ], f"majors were lost with the earlier run {earlier_state}"


def test_the_ticks_come_from_the_listing_once_it_exists(client, submitter, reviewer):
    """The catalog is the durable record, independent of any run's status."""
    from lumina.hardware.models import ListingVersion
    from lumina.results.models import TestRun

    first = _run(submitter, version_id="9.6")
    _speaks_for_the_vendor(submitter)
    client.force_login(submitter)
    _post_all_three(client, first)
    services.submit_for_review(TestRun.objects.get(pk=first.pk), by=submitter)
    services.approve_run(TestRun.objects.get(pk=first.pk), by=reviewer)
    first.refresh_from_db()
    assert first.listing_system is not None

    majors = set(
        ListingVersion.objects.filter(listing_system=first.listing_system)
        .values_list("release__major", flat=True)
    )

    assert {8, 9, 10} <= majors, "approval did not record the declared releases"
    ticks = services.claimed_release_ticks(_run(submitter, version_id="10.2"))
    assert {"release_8", "release_9", "release_10"} <= set(ticks)


def test_the_sources_union_rather_than_narrow(client, submitter, reviewer):
    """A prefill must not narrow a claim. Two sources - the catalog listing and a sibling
    proposal - contribute majors, and the result holds both.

    This used to be about the lowest *minor* winning across sources, which took some setting
    up to make the two disagree. With hardware majors-only there is no floor to reconcile, and
    what is left worth asserting is that neither source is dropped.
    """
    from lumina.hardware.models import ListingVersion
    from lumina.results.models import TestRun

    first = _run(submitter, version_id="9.6")
    _speaks_for_the_vendor(submitter)
    client.force_login(submitter)
    _post(client, first, release_9="on")
    services.submit_for_review(TestRun.objects.get(pk=first.pk), by=submitter)
    services.approve_run(TestRun.objects.get(pk=first.pk), by=reviewer)
    first.refresh_from_db()
    listing = first.listing_system
    assert ListingVersion.objects.filter(
        listing_system=listing, release__major=9
    ).exists(), "the listing should hold AlmaLinux 9"

    # A second, unapproved run of the same machine adds a major of its own.
    second = _run(submitter, version_id="10.2")
    _post(client, second, release_9="on", release_10="on")

    ticks = services.claimed_release_ticks(_run(submitter, version_id="9.6"))

    assert ticks["release_9"] is True, "from the listing"
    assert ticks["release_10"] is True, "from the sibling proposal"
    assert not any(key.startswith("release_minor") for key in ticks)


def test_a_catalogued_machine_prefills_even_with_no_runs_of_your_own(
    client, submitter, reviewer
):
    """The listing source, which nothing else covers.

    Somebody else's approved run put this machine in the catalog declaring 8, 9, and
    10. A first-time submitter has no runs of their own to borrow from, so without
    the listing there is nothing to prefill from and the form would come back
    claiming only the release they just tested.
    """
    from lumina.results.models import TestRun

    owner = User.objects.create_user("first-mover", password="pw")
    theirs = _run(owner, version_id="9.6")
    client.force_login(owner)
    _post_all_three(client, theirs)
    services.submit_for_review(TestRun.objects.get(pk=theirs.pk), by=owner)
    services.approve_run(TestRun.objects.get(pk=theirs.pk), by=reviewer)

    mine = _run(submitter, version_id="10.2")
    assert not services.sibling_runs(
        mine, [status for status, _ in TestRun.STATUS_CHOICES]
    ).exists(), "this test needs the submitter to have no runs of their own"

    ticks = services.claimed_release_ticks(mine)

    assert {"release_8", "release_9", "release_10"} <= set(ticks)


def test_another_submitters_unapproved_claims_are_not_consulted(client, submitter):
    """Somebody else's *pending* run of the same model is an independent
    submission, and borrowing its claims would put words in this submitter's mouth.

    Once such a run is approved the machine is in the catalog, and then the
    listing legitimately does prefill - see
    ``test_a_catalogued_machine_prefills_even_with_no_runs_of_your_own``. The line
    is review, not authorship.
    """
    stranger = User.objects.create_user("stranger", password="pw")
    theirs = _run(stranger)
    client.force_login(stranger)
    _post_all_three(client, theirs)

    mine = _run(submitter, version_id="10.2")

    assert services.claimed_release_ticks(mine) == {}
