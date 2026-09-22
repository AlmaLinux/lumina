"""The catalog's front page leads with what was certified most recently.

It was alphabetical, inherited from ``HardwareListing.Meta.ordering``, which is right for a
picker and wrong for a browse page: the first screen never changed, so somebody coming back to
see what was new saw exactly what they saw last month. A certification catalog whose front page
is a constant is telling its readers not to return to it.

``last_certified_at`` is derived from the listing's attestations by
``recompute_listing_levels``, beside ``validation_level`` and ``attestation_count`` and for the
same reason: browse sorts on it, and a Max() over a join on the busiest page in the catalog,
under filters that already join, is paid on every render and every count query.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone

from lumina.core.certification import ValidationLevel
from lumina.hardware.models import CommunityAttestation, ListingVersion, Submission, System
from lumina.hardware.services import recompute_listing_levels
from lumina.releases.models import AlmaLinuxRelease
from lumina.vendors.models import Vendor

pytestmark = pytest.mark.django_db


@pytest.fixture
def release():
    return AlmaLinuxRelease.objects.get_or_create(
        major=9, defaults={"supported": True})[0]


@pytest.fixture
def attester():
    return User.objects.create_user("browse-attester")


@pytest.fixture
def vendor():
    return Vendor.objects.create(name="Ordering Co", published=True)


def certified(vendor, attester, release, name: str, *, when) -> System:
    """A published system whose newest attestation is ``when``."""
    system = System.objects.create(vendor=vendor, name=name, published=True)
    version = ListingVersion.objects.create(
        listing_system=system, release=release, source=ListingVersion.SOURCE_RUN)
    submission = Submission.objects.create(
        submitter=attester, listing_system=system,
        claimed_validation_level=ValidationLevel.COMMUNITY,
        status=Submission.STATUS_APPROVED,
    )
    attestation = CommunityAttestation.objects.create(
        version=version, listing_system=system, submission=submission,
        attested_by=attester, level=ValidationLevel.COMMUNITY,
    )
    # auto_now_add, so the stamp has to be written after the fact.
    CommunityAttestation.objects.filter(pk=attestation.pk).update(created_at=when)
    recompute_listing_levels(system)
    return system


def browse_order(client) -> list[str]:
    response = client.get(reverse("hardware:systems"))
    return [listing.name for listing in response.context["listings"]]


# --- the derived column ----------------------------------------------------------


def test_the_stamp_comes_from_the_newest_attestation(vendor, attester, release):
    """A machine re-validated last week is current news whether or not its first attestation
    is years old, so the newest wins rather than the first."""
    now = timezone.now()
    system = certified(vendor, attester, release, "Box A", when=now - timedelta(days=400))

    alma10 = AlmaLinuxRelease.objects.get_or_create(
        major=10, defaults={"supported": True})[0]
    version = ListingVersion.objects.create(
        listing_system=system, release=alma10, source=ListingVersion.SOURCE_RUN)
    submission = Submission.objects.create(
        submitter=attester, listing_system=system,
        claimed_validation_level=ValidationLevel.COMMUNITY,
        status=Submission.STATUS_APPROVED,
    )
    fresh = CommunityAttestation.objects.create(
        version=version, listing_system=system, submission=submission,
        attested_by=User.objects.create_user("second-attester"),
        level=ValidationLevel.COMMUNITY,
    )
    CommunityAttestation.objects.filter(pk=fresh.pk).update(created_at=now)
    recompute_listing_levels(system)

    system.refresh_from_db()
    assert system.last_certified_at == now


def test_a_listing_nothing_has_certified_has_no_stamp(vendor):
    """A vendor's declared support is not a certification, and dating it as one would float an
    unproven claim to the top of the page."""
    system = System.objects.create(vendor=vendor, name="Declared Only", published=True)

    recompute_listing_levels(system)

    system.refresh_from_db()
    assert system.last_certified_at is None


def test_losing_an_attestation_moves_the_stamp_back(vendor, attester, release):
    """Derived, not stamped once. ``recompute_listing_levels`` exists because removing evidence
    has to lower a listing's standing, and the date is standing too."""
    system = certified(vendor, attester, release, "Box A", when=timezone.now())
    CommunityAttestation.objects.filter(listing_system=system).delete()

    recompute_listing_levels(system)

    system.refresh_from_db()
    assert system.last_certified_at is None


# --- the page ---------------------------------------------------------------------


def test_the_newest_certification_is_first(client, vendor, attester, release):
    """The report: alphabetical meant the front page never changed."""
    now = timezone.now()
    certified(vendor, attester, release, "Aardvark", when=now - timedelta(days=30))
    certified(vendor, attester, release, "Zebra", when=now)

    assert browse_order(client)[:2] == ["Zebra", "Aardvark"]


def test_uncertified_listings_sort_last(client, vendor, attester, release):
    """Where a listing nothing has certified belongs, on a page about what was certified.

    SQLite and MariaDB both sort NULLs last on DESC, so ``nulls_last`` changes nothing on the
    backends this project runs - it is spelled out so the query states the intent rather than
    inheriting it.
    """
    certified(vendor, attester, release, "Certified", when=timezone.now())
    System.objects.create(vendor=vendor, name="Aaa Uncertified", published=True)

    order = browse_order(client)

    assert order[0] == "Certified"
    assert order[-1] == "Aaa Uncertified"


def test_name_breaks_the_tie(client, vendor, attester, release):
    """Two listings certified in the same instant need a defined order, because pagination over
    an unstable sort drops and repeats rows between pages.

    Asserted on the query rather than on the rows it returned: with the tie-break gone the
    order is undefined, not reversed, and a database that happens to hand back the right two
    names would report a guarantee that is not there.
    """
    now = timezone.now()
    certified(vendor, attester, release, "Bravo", when=now)
    certified(vendor, attester, release, "Alpha", when=now)

    response = client.get(reverse("hardware:systems"))

    assert "name" in response.context["listings"].query.order_by
    assert browse_order(client)[:2] == ["Alpha", "Bravo"]


def test_the_picker_ordering_is_left_alone(vendor, attester, release):
    """Only the browse page changed. ``Meta.ordering`` is alphabetical, which is what a
    dropdown, an admin list, and a dashboard table want - somebody looking for a name."""
    certified(vendor, attester, release, "Zebra", when=timezone.now())
    certified(vendor, attester, release, "Aardvark", when=timezone.now() - timedelta(days=1))

    assert [s.name for s in System.objects.all()][:2] == ["Aardvark", "Zebra"]


def test_the_migration_backfills_an_existing_catalog(vendor, attester, release):
    """Without it every listing already in a catalog sorts as never certified, so the ordering
    this was added for shows nothing until somebody certifies something new - on the one page
    where the existing catalog is the whole content.

    Calls the migration's own function rather than replaying the migration: the column is there
    by the time any test runs, so what is left to check is that the function fills it.
    """
    import importlib

    from django.apps import apps as live_apps

    # import_module by string: a module whose name starts with a digit cannot be reached with
    # an import statement.
    migration = importlib.import_module(
        "lumina.hardware.migrations.0006_listing_last_certified_at")

    system = certified(vendor, attester, release, "Existing", when=timezone.now())
    stamp = system.last_certified_at
    System.objects.filter(pk=system.pk).update(last_certified_at=None)

    migration.backfill(live_apps, None)

    system.refresh_from_db()
    assert system.last_certified_at == stamp
