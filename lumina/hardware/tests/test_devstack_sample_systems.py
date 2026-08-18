"""The real certified systems the devstack catalog is seeded with.

These are four named machines from four named manufacturers, published at
almalinux.org/certification/ecosystem-catalog, so the seed data makes claims about third
parties. The invariants here are what keep those claims matching the source, and they are the
kind a well-meaning later edit would break without noticing.

They replaced an invented Dell PowerEdge R750 and Supermicro SuperServer, which was worse than
it sounds: a devstack is where people learn what the catalog looks like, and it was showing them
a shape with nothing real in it.

The one that matters most:

- **The tier follows who ran the tests, not who makes the hardware.** Fsas Technologies ran their
  own, so those two are vendor-validated. The two Supermicro systems say "Tests Run By: AlmaLinux
  OS Foundation", which is the almalinux tier. Calling those vendor-validated is precisely the
  conflation ``results.services.effective_level`` exists to prevent, and its docstring carries the
  measurements from when this system got it wrong in the other direction.
"""
from __future__ import annotations

import pytest

from lumina.core.certification import ValidationLevel
from lumina.core.management.commands.seed_devstack import Command
from lumina.hardware.models import ComponentKind, System

pytestmark = pytest.mark.django_db

# (name, vendor, tier, the AlmaLinux majors the catalog says are certified, CPU listing)
CERTIFIED = [
    ("PRIMERGY RX2540 M8 Rack Server", "Fsas Technologies", ValidationLevel.VENDOR,
     {9}, "Intel® Xeon® 6 Processors"),
    ("PRIMERGY RX2530 M8 Rack Server", "Fsas Technologies", ValidationLevel.VENDOR,
     {9}, "Intel® Xeon® 6 Processors"),
    ("A+ Server AS-2124BT-HNTR", "Supermicro", ValidationLevel.ALMALINUX,
     {8, 9}, "AMD EPYC™ 7003 Series"),
    ("CloudDC SuperServer SYS-621C-TN12R", "Supermicro", ValidationLevel.ALMALINUX,
     {8, 9}, "Intel® Xeon® Scalable 4th Generation"),
]


@pytest.fixture
def seeded():
    """Only the pieces the sample listings need, so the test stays offline.

    Every vendor in ``_SAMPLE_VENDORS`` carries a blank logo URL and therefore gets a locally
    drawn placeholder; a seeded vendor with a remote logo would fetch it over HTTP.
    """
    command = Command()
    command._seed_taxonomy()
    command._seed_alma_releases()
    command._seed_vendors()
    command._seed_sample_cpus()
    command._seed_sample_listings()
    return command


def listing(name: str) -> System:
    return System.objects.get(name=name)


@pytest.mark.parametrize(("name", "vendor", "tier", "majors", "cpu"), CERTIFIED)
def test_each_certified_system_is_seeded_and_published(seeded, name, vendor, tier, majors, cpu):
    system = listing(name)

    assert system.published
    assert system.vendor.name == vendor


@pytest.mark.parametrize(("name", "vendor", "tier", "majors", "cpu"), CERTIFIED)
def test_the_tier_is_who_ran_the_tests(seeded, name, vendor, tier, majors, cpu):
    """Derived from the attestations the seeder writes, not from the literal in the spec, so a
    tier here is one the catalog could actually justify."""
    assert listing(name).validation_level == tier


def test_no_system_the_foundation_tested_claims_the_vendor_did(seeded):
    """The invariant behind the parametrized case above, stated once so it survives somebody
    editing the table. "Vendor-validated" means the company that makes the thing validated it."""
    foundation_tested = ["A+ Server AS-2124BT-HNTR", "CloudDC SuperServer SYS-621C-TN12R"]

    for name in foundation_tested:
        assert listing(name).validation_level != ValidationLevel.VENDOR


@pytest.mark.parametrize(("name", "vendor", "tier", "majors", "cpu"), CERTIFIED)
def test_the_certified_releases_are_the_ones_the_catalog_lists(
        seeded, name, vendor, tier, majors, cpu):
    """Both directions. Missing a major understates a real certification; inventing one puts a
    claim in the catalog that its source does not make."""
    system = listing(name)

    assert {version.release.major for version in system.versions.all()} == majors


@pytest.mark.parametrize(("name", "vendor", "tier", "majors", "cpu"), CERTIFIED)
def test_every_certified_release_has_evidence_behind_it(
        seeded, name, vendor, tier, majors, cpu):
    """A release row with no attestation renders as declared rather than proven, which would
    quietly downgrade a real certification to a manufacturer's say-so."""
    for version in listing(name).versions.all():
        assert version.attestations.exists(), f"{name} on {version.release.major}"


@pytest.mark.parametrize(("name", "vendor", "tier", "majors", "cpu"), CERTIFIED)
def test_each_system_names_the_processor_it_was_certified_on(
        seeded, name, vendor, tier, majors, cpu):
    """The CPU facet and the "systems running this processor" panel are only worth having if the
    seeded systems are actually attached to something."""
    attached = listing(name).cpus.all()

    assert [component.name for component in attached] == [cpu]
    assert all(component.kind == ComponentKind.cpu.value for component in attached)


@pytest.mark.parametrize(("name", "vendor", "tier", "majors", "cpu"), CERTIFIED)
def test_each_system_links_its_manufacturers_product_page(
        seeded, name, vendor, tier, majors, cpu):
    """A sample listing whose "Product specs" link goes nowhere teaches the reader that the
    field is decorative."""
    assert listing(name).vendor_spec_url.startswith("https://")


def test_the_invented_machines_are_gone(seeded):
    """Named, because both were load-bearing in people's heads: every screenshot, bug report,
    and test fixture in this project says PowerEdge R750."""
    assert not System.objects.filter(name__in=[
        "PowerEdge R750", "SuperServer SYS-221H-TN24R",
    ]).exists()


def test_nothing_else_is_seeded_as_a_system(seeded):
    """The catalog's front page is these four. An extra sample machine added later without a
    source is the thing this module exists to catch."""
    assert System.objects.count() == len(CERTIFIED)
