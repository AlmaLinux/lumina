"""The certifications a production catalog is launched with.

``seed_certified_systems`` records the four systems published at
almalinux.org/certification/ecosystem-catalog. They are real machines belonging to real
companies, so the seed makes claims about third parties and these are what keep those claims
matching the source - the same job ``software/tests/test_ecosystem_seed.py`` does for the
software listings, and for the same reason.

The one that matters most is the tier: it is who the catalog says *ran the tests*, not who makes
the hardware. Fsas Technologies ran their own; the two Supermicro systems say "Tests Run By:
AlmaLinux OS Foundation". Recording the second pair as vendor-validated would be the exact
conflation ``results.services.effective_level`` exists to prevent.

A command rather than a migration, which these tests are the reason for: as a migration it put
four published listings into every test database and broke 64 tests across twenty modules -
everything that counted the catalog or resolved a vendor that was not supposed to exist yet.
Releases are infrastructure and migrate; content is a decision, and is run once.
"""
from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command

from lumina.core.certification import ValidationLevel
from lumina.hardware.models import CommunityAttestation, Submission, System
from lumina.vendors.models import Vendor

pytestmark = pytest.mark.django_db

SIG_USERNAME = "almalinux-certification-sig"

# (name, vendor, tier, the AlmaLinux majors the catalog certifies)
CERTIFIED = [
    ("PRIMERGY RX2540 M8 Rack Server", "Fsas Technologies", ValidationLevel.VENDOR, {9}),
    ("PRIMERGY RX2530 M8 Rack Server", "Fsas Technologies", ValidationLevel.VENDOR, {9}),
    ("A+ Server AS-2124BT-HNTR", "Supermicro", ValidationLevel.ALMALINUX, {8, 9}),
    ("CloudDC SuperServer SYS-621C-TN12R", "Supermicro", ValidationLevel.ALMALINUX, {8, 9}),
]


@pytest.fixture(autouse=True)
def seeded():
    """Run the command, which is the only thing that creates any of this."""
    call_command("seed_certified_systems", verbosity=0)


def listing(name: str) -> System:
    return System.objects.get(name=name)


# --- what a fresh production database holds --------------------------------------


@pytest.mark.parametrize(("name", "vendor", "tier", "majors"), CERTIFIED)
def test_each_certified_system_is_published(name, vendor, tier, majors):
    system = listing(name)

    assert system.published
    assert system.vendor.name == vendor


@pytest.mark.parametrize(("name", "vendor", "tier", "majors"), CERTIFIED)
def test_the_tier_is_who_ran_the_tests(name, vendor, tier, majors):
    assert listing(name).validation_level == tier


def test_no_system_the_foundation_tested_claims_the_vendor_did():
    """Stated once so it survives somebody editing the table above. "Vendor-validated" means
    the company that makes the thing validated it."""
    for name in ("A+ Server AS-2124BT-HNTR", "CloudDC SuperServer SYS-621C-TN12R"):
        assert listing(name).validation_level != ValidationLevel.VENDOR


@pytest.mark.parametrize(("name", "vendor", "tier", "majors"), CERTIFIED)
def test_the_certified_releases_are_the_ones_the_catalog_lists(name, vendor, tier, majors):
    """Both directions: missing a major understates a real certification, and inventing one
    puts a claim in the catalog that its source does not make."""
    assert {version.release.major for version in listing(name).versions.all()} == majors


@pytest.mark.parametrize(("name", "vendor", "tier", "majors"), CERTIFIED)
def test_every_release_row_carries_its_own_tier_and_evidence(name, vendor, tier, majors):
    """A release row with no attestation renders as declared rather than proven, which would
    quietly downgrade a real certification to a manufacturer's say-so."""
    for version in listing(name).versions.all():
        assert version.validation_level == tier
        assert version.attestations.exists()
        assert version.source == "run"


@pytest.mark.parametrize(("name", "vendor", "tier", "majors"), CERTIFIED)
def test_the_derived_columns_agree_with_the_evidence(name, vendor, tier, majors):
    """``validation_level`` and ``attestation_count`` are normally recomputed from the rows
    beneath them; a migration writes them directly, so they can disagree here and nowhere
    else."""
    system = listing(name)

    assert system.attestation_count == len(majors)
    assert system.attestation_count == CommunityAttestation.objects.filter(
        listing_system=system).count()


@pytest.mark.parametrize(("name", "vendor", "tier", "majors"), CERTIFIED)
def test_each_system_links_its_manufacturers_product_page(name, vendor, tier, majors):
    assert listing(name).vendor_spec_url.startswith("https://")


# --- who the record is attributed to ---------------------------------------------


def test_the_records_belong_to_a_service_account_and_not_a_person():
    """``attested_by`` is required and PROTECTed, so these had to be attributed to somebody.
    A person would then own claims they never personally made, permanently."""
    attestations = CommunityAttestation.objects.filter(listing_system__isnull=False)

    assert attestations.exists()
    assert {a.attested_by.username for a in attestations} == {SIG_USERNAME}


def test_the_service_account_cannot_be_signed_in_as():
    """It exists to be pointed at, not used. An inactive account is refused by every auth
    backend, and the password is unusable besides."""
    from django.contrib.auth.models import User

    sig = User.objects.get(username=SIG_USERNAME)

    assert not sig.is_active
    assert not sig.has_usable_password()
    assert not sig.is_staff and not sig.is_superuser


def test_every_attestation_names_the_submission_it_came_from():
    """``attestation_exactly_one_source`` demands one of submission or run, and there is no
    run: the logs are Phoronix output, not an alma-certify bundle."""
    for attestation in CommunityAttestation.objects.filter(listing_system__isnull=False):
        assert attestation.submission_id is not None
        assert attestation.test_run_id is None


def test_the_submissions_cite_exactly_the_releases_they_certified():
    """``Submission.approve`` reads ``cited_releases``, so a seeded submission that cited
    nothing would be a row claiming a tier over no release at all."""
    for name, _vendor, _tier, majors in CERTIFIED:
        submission = Submission.objects.get(listing_system=listing(name))
        assert {r.major for r in submission.cited_releases.all()} == majors


# --- what it deliberately does not do --------------------------------------------


def test_the_vendors_are_verified_but_unclaimed():
    """Verification records that these are named partners in a published program. Ownership is
    a person proving they represent the company, and nobody has - so the claim flow stays open
    to them."""
    for name in ("Fsas Technologies", "Supermicro"):
        vendor = Vendor.objects.get(name=name)
        assert vendor.verified and vendor.published
        assert not vendor.memberships.exists()


def test_no_system_links_a_cpu_that_would_404():
    """The detail page links a listing's CPUs unconditionally, and the reference families are
    seeded unpublished. Attaching one would put a link to a 404 on a production page."""
    for name, _vendor, _tier, _majors in CERTIFIED:
        unpublished = listing(name).cpus.filter(published=False)
        assert not unpublished.exists()


def test_the_catalog_is_not_empty_on_a_fresh_install():
    """The whole point. A certification catalog that opens empty asks its first visitor to take
    its word that it will be useful later."""
    assert System.objects.filter(published=True).count() >= len(CERTIFIED)


# --- running it, and running it again --------------------------------------------


def test_a_second_run_records_nothing_new():
    """It is run by hand on a live catalog, so somebody will run it twice."""
    out = StringIO()
    call_command("seed_certified_systems", stdout=out)

    assert "Nothing to record" in out.getvalue()
    assert System.objects.filter(name__in=[c[0] for c in CERTIFIED]).count() == len(CERTIFIED)


def test_it_does_not_resurrect_attestations_on_a_listing_somebody_kept():
    """Only ever adds, and only whole listings. A catalog where somebody has since corrected a
    tier must not have this command argue with them on the next run."""
    system = listing("A+ Server AS-2124BT-HNTR")
    CommunityAttestation.objects.filter(listing_system=system).delete()
    system.refresh_from_db()

    call_command("seed_certified_systems", verbosity=0)

    assert not CommunityAttestation.objects.filter(listing_system=system).exists()


def test_a_dry_run_writes_nothing(db):
    System.objects.filter(name__in=[c[0] for c in CERTIFIED]).delete()
    out = StringIO()

    call_command("seed_certified_systems", "--dry-run", stdout=out)

    assert "Would record 4" in out.getvalue()
    assert not System.objects.filter(name__in=[c[0] for c in CERTIFIED]).exists()
