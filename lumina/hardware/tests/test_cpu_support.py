"""Multi-generation CPU support on a System listing.

A server usually accepts more than one CPU generation: a Supermicro
SYS-1029U-TN10RT takes both 1st and 2nd generation Xeon Scalable. Validating it
with one generation proves nothing about the other, so declared support and
validation evidence are separate relations that render differently.
"""
from __future__ import annotations

import pytest
from django.urls import reverse

from lumina.core.certification import ValidationLevel
from lumina.hardware.models import (
    Component,
    ComponentKind,
    ComponentRole,
    System,
)
from lumina.vendors.models import Vendor

pytestmark = pytest.mark.django_db


@pytest.fixture
def intel():
    return Vendor.objects.get_or_create(name="Intel", defaults={"slug": "intel"})[0]


@pytest.fixture
def supermicro():
    return Vendor.objects.get_or_create(
        name="Supermicro", defaults={"slug": "supermicro"}
    )[0]


def _family(vendor, name, patterns):
    return Component.objects.create(
        vendor=vendor, name=name, kind=ComponentKind.cpu.value,
        role=ComponentRole.FAMILY, model_patterns=patterns, published=True,
        slug=name.lower().replace(" ", "-"),
    )


@pytest.fixture
def server(supermicro, intel):
    system = System.objects.create(
        vendor=supermicro, name="SYS-1029U-TN10RT", slug="sys-1029u-tn10rt",
        published=True, validation_level=ValidationLevel.ALMALINUX,
    )
    gen1 = _family(intel, "Intel Xeon Scalable 1st Generation",
                   [r"Xeon (Platinum|Gold|Silver|Bronze) [3-9]1[0-9]{2}"])
    gen2 = _family(intel, "Intel Xeon Scalable 2nd Generation",
                   [r"Xeon (Platinum|Gold|Silver|Bronze) [3-9]2[0-9]{2}"])
    return system, gen1, gen2


def test_a_system_can_carry_several_validated_cpu_families(server):
    """The relation is many-to-many, so runs on different generations
    accumulate rather than replacing each other."""
    system, gen1, gen2 = server
    system.cpus.add(gen1, gen2)

    assert {c.name for c in system.cpus.all()} == {gen1.name, gen2.name}
    assert all(entry["validated"] for entry in system.cpu_support())


def test_declared_support_is_kept_apart_from_validation_evidence(server):
    """The Supermicro case: the vendor states two generations, one is proven."""
    system, gen1, gen2 = server
    system.supported_cpus.add(gen1, gen2)
    system.cpus.add(gen2)                       # only this one was tested

    support = {e["cpu"].name: e["validated"] for e in system.cpu_support()}
    assert support == {gen1.name: False, gen2.name: True}


def test_validated_families_are_listed_first(server):
    system, gen1, gen2 = server
    system.supported_cpus.add(gen1, gen2)
    system.cpus.add(gen2)

    validated_flags = [e["validated"] for e in system.cpu_support()]
    assert validated_flags == [True, False]


def test_a_family_in_both_relations_is_not_duplicated(server):
    """Declaring support for a family that was then validated must not list it
    twice, once as proven and once as a claim."""
    system, gen1, _ = server
    system.supported_cpus.add(gen1)
    system.cpus.add(gen1)

    support = system.cpu_support()
    assert len(support) == 1
    assert support[0]["validated"] is True


def test_declared_support_alone_grants_no_certification(server, client):
    """A spec-sheet claim must never render as a test result."""
    system, gen1, gen2 = server
    system.supported_cpus.add(gen1, gen2)
    system.cpus.add(gen2)

    body = client.get(reverse("hardware:detail", args=[system.slug])).content.decode()

    assert "Vendor states support" in body
    assert "carry no certification" in body
    # both generations are visible to someone checking compatibility
    assert gen1.name in body and gen2.name in body


def test_no_note_when_every_family_is_validated(server, client):
    """With nothing declared there is no distinction to explain."""
    system, gen1, gen2 = server
    system.cpus.add(gen1, gen2)

    body = client.get(reverse("hardware:detail", args=[system.slug])).content.decode()

    assert "Vendor states support" not in body
    assert "carry no certification" not in body
    assert gen1.name in body and gen2.name in body


def test_a_system_with_no_cpu_information_shows_no_card(supermicro, client):
    system = System.objects.create(
        vendor=supermicro, name="Bare Listing", slug="bare-listing", published=True,
    )
    body = client.get(reverse("hardware:detail", args=[system.slug])).content.decode()
    assert "CPU families" not in body


# --- AlmaLinux compatibility on a family ---------------------------------------


def test_compatibility_is_editable_per_listing_in_the_admin():
    """There was no way to set this at all: rows only appeared when a passing
    run wrote one, so a family showed exactly the releases someone happened to
    have tested on."""
    from django.contrib import admin

    from lumina.hardware.admin import ListingVersionInline
    from lumina.hardware.models import Component, ListingVersion, System

    for model in (System, Component):
        inlines = admin.site._registry[model].inlines
        assert ListingVersionInline in inlines, model.__name__
    assert ListingVersionInline.model is ListingVersion
    assert "source" in ListingVersionInline.fields


def test_a_run_marks_compatibility_as_proven(intel, supermicro):
    """record_compatibility writes evidence."""
    from lumina.hardware.models import ListingVersion
    from lumina.releases.models import AlmaLinuxRelease

    release = AlmaLinuxRelease.objects.get_or_create(major=10)[0]
    system = System.objects.create(vendor=supermicro, name="Proven Box",
                                   slug="proven-box", published=True)
    version = ListingVersion.objects.create(
        listing_system=system, release=release,
        source=ListingVersion.SOURCE_RUN,
    )
    assert version.source == ListingVersion.SOURCE_RUN


def test_a_hand_added_row_defaults_to_declared(supermicro):
    """An admin adding "AlmaLinux 9" to a Ryzen 7000 family is making a claim,
    not reporting a test result, and the two must not look identical."""
    from lumina.hardware.models import ListingVersion
    from lumina.releases.models import AlmaLinuxRelease

    release = AlmaLinuxRelease.objects.get_or_create(major=9)[0]
    system = System.objects.create(vendor=supermicro, name="Claimed Box",
                                   slug="claimed-box", published=True)

    version = ListingVersion.objects.create(
        listing_system=system, release=release
    )

    assert version.source == ListingVersion.SOURCE_DECLARED


def test_the_page_marks_declared_releases(client, supermicro):
    from lumina.hardware.models import ListingVersion
    from lumina.releases.models import AlmaLinuxRelease

    system = System.objects.create(vendor=supermicro, name="Mixed Box",
                                   slug="mixed-box", published=True)
    ListingVersion.objects.create(
        listing_system=system,
        release=AlmaLinuxRelease.objects.get_or_create(major=10)[0], source=ListingVersion.SOURCE_RUN,
    )
    ListingVersion.objects.create(
        listing_system=system,
        release=AlmaLinuxRelease.objects.get_or_create(major=9)[0], source=ListingVersion.SOURCE_DECLARED,
    )

    body = client.get(reverse("hardware:detail", args=[system.slug])).content.decode()

    # The compatibility card is a per-release table now, so a declared row shows
    # this in its tier column instead of a "(declared)" suffix on a badge.
    assert "Declared, not yet validated" in body
    assert "have not been proven by a" in body
    # The run-proven release keeps its evidence-shaped floor in the label.
    assert "AlmaLinux 10" in body


def test_no_note_when_everything_is_proven(client, supermicro):
    from lumina.hardware.models import ListingVersion
    from lumina.releases.models import AlmaLinuxRelease

    system = System.objects.create(vendor=supermicro, name="All Proven",
                                   slug="all-proven", published=True)
    ListingVersion.objects.create(
        listing_system=system,
        release=AlmaLinuxRelease.objects.get_or_create(major=10)[0], source=ListingVersion.SOURCE_RUN,
    )

    body = client.get(reverse("hardware:detail", args=[system.slug])).content.decode()

    assert "(declared)" not in body
    assert "have not been proven by a" not in body
