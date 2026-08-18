"""Systems listed on a component page, so the catalog navigates both ways.

The family/model rollup is the substance here: certification attaches CPU
*families* while benchmarks record *models*, so a model page that only looked
at its own relations would sit empty while its family listed a dozen machines.
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
def dell():
    return Vendor.objects.get_or_create(name="Dell Inc.", defaults={"slug": "dell"})[0]


def _component(vendor, name, kind=ComponentKind.cpu, role=ComponentRole.MODEL,
               patterns=None):
    return Component.objects.create(
        vendor=vendor, name=name, kind=kind.value, role=role,
        model_patterns=patterns or [], published=True,
        slug=name.lower().replace(" ", "-").replace("(", "").replace(")", ""),
    )


def _system(vendor, name, published=True):
    return System.objects.create(
        vendor=vendor, name=name, slug=name.lower().replace(" ", "-"),
        published=published, validation_level=ValidationLevel.ALMALINUX,
    )


@pytest.fixture
def catalog(intel, dell):
    """The real seeded family, not a copy of it.

    Creating a second "Intel Xeon Scalable 4th Generation" would make
    ``resolved_family`` pick whichever row the database returned first, so this
    uses the one the seed migration installed and publishes it as an attested
    family would be.
    """
    family = Component.objects.get(
        name="Intel Xeon Scalable 4th Generation",
        kind=ComponentKind.cpu.value, role=ComponentRole.FAMILY,
    )
    family.published = True
    family.save(update_fields=["published"])
    model = _component(family.vendor, "Xeon Gold 6430")
    system = _system(dell, "PowerEdge R760")
    return family, model, system


def test_a_cpu_family_lists_the_systems_it_is_validated_in(catalog):
    family, _, system = catalog
    system.cpus.add(family)

    used = family.used_in_systems()
    assert [(e["system"].name, e["relation"]) for e in used] == [
        ("PowerEdge R760", "validated")
    ]


def test_a_model_inherits_the_systems_of_its_family(catalog):
    """The rollup that makes a model page useful: nobody attaches "Xeon Gold
    6430" to a system, they attach its generation."""
    family, model, system = catalog
    system.cpus.add(family)

    assert model.resolved_family() == family
    assert [e["system"].name for e in model.used_in_systems()] == ["PowerEdge R760"]


def test_a_family_picks_up_systems_recorded_against_one_of_its_models(catalog):
    """The reverse rollup: some runs attached the specific model."""
    family, model, system = catalog
    system.cpus.add(model)

    assert [e["system"].name for e in family.used_in_systems()] == ["PowerEdge R760"]


def test_vendor_declared_support_is_labeled_as_such(catalog):
    family, _, system = catalog
    system.supported_cpus.add(family)

    used = family.used_in_systems()
    assert [(e["system"].name, e["relation"]) for e in used] == [
        ("PowerEdge R760", "supported")
    ]


def test_validated_outranks_declared_for_the_same_system(catalog):
    """A system that declares support and then proves it must appear once, as
    validated, not twice."""
    family, _, system = catalog
    system.supported_cpus.add(family)
    system.cpus.add(family)

    used = family.used_in_systems()
    assert len(used) == 1
    assert used[0]["relation"] == "validated"


def test_a_motherboard_lists_the_system_it_is_fitted_in(intel, dell):
    board = _component(dell, "0M83RH", kind=ComponentKind.motherboard)
    system = _system(dell, "PowerEdge R760")
    system.related_components.add(board)

    used = board.used_in_systems()
    assert [(e["system"].name, e["relation"]) for e in used] == [
        ("PowerEdge R760", "present")
    ]


def test_unpublished_systems_are_never_listed(catalog, dell):
    """An embargoed or draft machine must not be revealed by a component page."""
    family, _, published = catalog
    published.cpus.add(family)
    hidden = _system(dell, "Unreleased R790", published=False)
    hidden.cpus.add(family)

    names = [e["system"].name for e in family.used_in_systems()]
    assert names == ["PowerEdge R760"]
    assert "Unreleased R790" not in names


def test_the_component_page_renders_its_systems(catalog, client):
    family, _, system = catalog
    system.cpus.add(family)

    body = client.get(reverse("hardware:detail", args=[family.slug])).content.decode()

    assert "Used in systems" in body
    assert "PowerEdge R760" in body


def test_the_component_page_omits_the_card_when_nothing_uses_it(catalog, client):
    family, _, _ = catalog
    body = client.get(reverse("hardware:detail", args=[family.slug])).content.decode()
    assert "Used in systems" not in body


def test_the_api_exposes_the_systems_with_their_relation(catalog, client):
    family, _, system = catalog
    system.supported_cpus.add(family)

    data = client.get(f"/api/v1/components/{family.slug}/").json()

    assert data["used_in_systems"] == [{
        "name": "PowerEdge R760", "vendor": "Dell Inc.",
        "slug": "poweredge-r760", "relation": "supported",
        "validation_level": "almalinux",
    }]
