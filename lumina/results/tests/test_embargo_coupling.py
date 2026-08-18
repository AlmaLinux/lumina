"""Certification is withheld while a run is embargoed.

An embargoed run is invisible to the public by design, with no placeholder that
would reveal unreleased hardware. Approving one used to publish its System
listing anyway: the run hid correctly while the catalog entry went live with an
attestation, so the machine was discoverable from /systems/, from a component
page, and from the API. The coupling now happens on the release date instead.
"""
from __future__ import annotations

import datetime

import pytest
from django.contrib.auth.models import Group, User

from lumina.hardware.models import ListingVersion, System
from lumina.releases.models import AlmaLinuxRelease
from lumina.results import ingest, services
from lumina.results.tests import factories as f
from lumina.results.tests.helpers import release
from lumina.vendors.models import Vendor

pytestmark = pytest.mark.django_db


@pytest.fixture
def submitter():
    return User.objects.create_user("embargo-sub", email="es@example.com")


@pytest.fixture
def reviewer():
    user = User.objects.create_user("embargo-rev", email="er@example.com")
    group, _ = Group.objects.get_or_create(name="reviewer")
    user.groups.add(group)
    return user


def _embargoed_run(submitter, reviewer, *, publish_after="2027-01-01"):
    Vendor.objects.get_or_create(name="Dell Inc.", defaults={"slug": "dell-inc"})
    report = f.make_report(
        run_types=["validate"],
        results=[f.validate_result("validate.cpu.functional")],
    )
    report["run"]["pre_release"] = True
    report["run"]["publish_after"] = publish_after
    run = ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(f.build_bundle(report)),
        source="api",
    )
    run.listing_proposal = {"vendor_name": "Dell Inc.", "name": "Unreleased R790"}
    run.save(update_fields=["listing_proposal"])
    services.create_listings_from_run(run, by=reviewer)
    return run


def test_approving_an_embargoed_run_does_not_publish_its_listing(submitter, reviewer):
    run = _embargoed_run(submitter, reviewer)
    services.approve_run(release(run), by=reviewer)

    run.refresh_from_db()
    system = System.objects.get(name="Unreleased R790")
    assert run.is_embargoed
    assert run.published_at is None          # the run is hidden, as before
    assert system.published is False         # and so is the hardware
    assert system.attestation_count == 0


def test_an_embargoed_run_does_not_publish_a_listing_it_joins(submitter, reviewer):
    """The withholding guard was in ``approve_run``, one branch away from the other caller.

    Every test around this one hands ``approve_run`` a run that already has a listing, so it
    takes the "already linked" branch where the guard lives. The other branch calls
    ``create_listings_from_run``, which attested on ``status == APPROVED`` alone - and
    ``approve_run`` sets that status and saves before calling it.

    It looked harmless: a listing being created for the first time has no release row yet, and
    ``_attest_one`` bails without one. Reuse is where it bit. An engineering sample reports a
    DMI product the shipping machine does not ("PowerEdge R790 EVT-3"), so nothing auto-links
    it at ingest and it reaches this branch - where it matches the existing "PowerEdge R790" by
    name, finds that listing's release row, and puts unreleased hardware in the catalog.
    """
    Vendor.objects.get_or_create(name="Dell Inc.", defaults={"slug": "dell-inc"})
    vendor = Vendor.objects.get(name="Dell Inc.")
    alma9, _ = AlmaLinuxRelease.objects.get_or_create(
        major=9, defaults={"supported": True},
    )
    system = System.objects.create(
        vendor=vendor, name="PowerEdge R790", published=False,
    )
    ListingVersion.objects.create(
        listing_system=system, release=alma9, source="declared",
    )

    report = f.make_report(
        run_types=["validate"],
        results=[f.validate_result("validate.cpu.functional")],
    )
    report["inventory"]["summary"]["system"]["product"] = "PowerEdge R790 EVT-3"
    report["run"]["pre_release"] = True
    report["run"]["publish_after"] = "2027-01-01"
    run = ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(f.build_bundle(report)),
        source="api",
    )
    assert run.listing_system_id is None, "the premise: nothing auto-linked it"
    run.listing_proposal = {"vendor_name": "Dell Inc.", "name": "PowerEdge R790"}
    run.save(update_fields=["listing_proposal"])

    services.approve_run(release(run), by=reviewer)

    system.refresh_from_db()
    assert system.published is False
    assert system.attestation_count == 0

    # ...and the release date still delivers it, so this is a delay, not a loss.
    services.publish_due_runs(today=datetime.date(2027, 1, 2))
    system.refresh_from_db()
    assert system.published is True
    assert system.attestation_count == 1


def test_an_embargoed_run_ties_no_components(submitter, reviewer):
    """Component pages list the systems they appear in, so a tie made early
    would expose the machine from the other direction too."""
    run = _embargoed_run(submitter, reviewer)
    services.approve_run(release(run), by=reviewer)

    assert list(run.listing_components.all()) == []
    system = System.objects.get(name="Unreleased R790")
    assert list(system.cpus.all()) == []


def test_the_release_date_publishes_the_listing_and_certifies_it(submitter, reviewer):
    """Everything the approval withheld lands at once on the release date."""
    run = _embargoed_run(submitter, reviewer)
    services.approve_run(release(run), by=reviewer)

    published = services.publish_due_runs(today=datetime.date(2027, 1, 2))

    assert [r.pk for r in published] == [run.pk]
    run.refresh_from_db()
    system = System.objects.get(name="Unreleased R790")
    assert run.published_at is not None
    assert system.published is True
    assert system.attestation_count == 1
    # the component ties the approval skipped are made now
    assert run.listing_components.exists()
    assert system.cpus.exists()


def test_nothing_is_released_before_the_date(submitter, reviewer):
    run = _embargoed_run(submitter, reviewer)
    services.approve_run(release(run), by=reviewer)

    assert services.publish_due_runs(today=datetime.date(2026, 12, 31)) == []
    assert System.objects.get(name="Unreleased R790").published is False


def test_an_unembargoed_run_still_certifies_immediately(submitter, reviewer):
    """The ordinary path must be unaffected by the withholding."""
    Vendor.objects.get_or_create(name="Dell Inc.", defaults={"slug": "dell-inc"})
    report = f.make_report(
        run_types=["validate"],
        results=[f.validate_result("validate.cpu.functional")],
    )
    run = ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(f.build_bundle(report)),
        source="api",
    )
    run.listing_proposal = {"vendor_name": "Dell Inc.", "name": "Released R760"}
    run.save(update_fields=["listing_proposal"])
    services.create_listings_from_run(run, by=reviewer)
    services.approve_run(release(run), by=reviewer)

    system = System.objects.get(name="Released R760")
    assert system.published is True
    assert system.attestation_count == 1
    assert system.cpus.exists()
