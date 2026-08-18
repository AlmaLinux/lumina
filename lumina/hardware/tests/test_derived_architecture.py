"""Architecture comes from the validation suite, not from a submitter.

The hardware taxonomy carried four facets. Three of them - Network, PCIe
Generation, and Storage - are gone: a facet only earns its place if it is filled in
consistently, and none of those were. A filter that is populated for some listings
and blank for others is worse than no filter, because an empty result reads as "no
such hardware" rather than "nobody recorded it".

Architecture is the one that survives, and it survives because it does not depend
on anybody filling it in: every validation run reports the kernel's arch, so an
approved run is authoritative. It is therefore marked ``derived_from_runs``, which
means:

- it is not offered on either submit form, because a submitter's answer could
  disagree with the machine's own kernel and there is no reason to let it
- approving a passing run binds it, the same place and the same way
  ``record_compatibility`` records the release

A listing with no runs simply has no architecture, exactly as it has no proven
release. That is honest rather than incomplete.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group

from lumina.hardware.forms import SubmissionForm
from lumina.hardware.models import ListingCategoryValue, System
from lumina.results import ingest, services
from lumina.results.tests import factories as f
from lumina.results.tests.helpers import release
from lumina.taxonomy.models import Category, CategoryValue
from lumina.vendors.models import Vendor

pytestmark = pytest.mark.django_db
User = get_user_model()

_RETIRED = ["network", "pcie-generation", "storage"]


@pytest.fixture(autouse=True)
def taxonomy():
    """Seeded, so the retired-category assertions test the seeder rather than an
    empty database - where they would pass without anything being removed."""
    from lumina.core.management.commands.seed_devstack import Command

    Command()._seed_taxonomy()


@pytest.fixture
def reviewer():
    user = User.objects.create_user("rev")
    user.groups.add(Group.objects.get_or_create(name="reviewer")[0])
    return user


@pytest.fixture
def architecture():
    return Category.objects.get(slug="architecture")


def _approved_run(submitter, system, reviewer, *, arch="aarch64"):
    inventory = f.default_inventory()
    report = f.make_report(
        run_types=["validate"],
        results=[f.validate_result("validate.cpu.functional")],
        inventory=inventory,
    )
    report["environment"]["os"]["arch"] = arch
    run = ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(f.build_bundle(report)),
        source="api",
    )
    run.listing_system = system
    run.save(update_fields=["listing_system"])
    services.approve_run(release(run, submitter), by=reviewer)
    return run


@pytest.fixture
def system():
    vendor = Vendor.objects.create(name="Ampere", published=True)
    return System.objects.create(vendor=vendor, name="Altra Box", published=True)


# --- the three retired facets -------------------------------------------------


@pytest.mark.parametrize("slug", _RETIRED)
def test_the_retired_categories_no_longer_exist(slug):
    """Removed rather than emptied. An empty category still renders a card."""
    assert not Category.objects.filter(slug=slug).exists()


@pytest.mark.parametrize("slug", _RETIRED)
def test_no_listing_is_still_bound_to_a_retired_category(slug):
    assert not ListingCategoryValue.objects.filter(
        value__category__slug=slug
    ).exists()


def test_the_retired_facets_are_gone_from_the_browse_panel(client, system):
    body = client.get("/hardware/systems/").content.decode()

    for slug in _RETIRED:
        assert f'data-category="{slug}"' not in body


def test_architecture_survives_as_a_facet(client, system):
    """It is the one hardware facet the suite fills in for itself."""
    body = client.get("/hardware/systems/").content.decode()

    assert 'data-category="architecture"' in body


# --- architecture is derived, not asked for -----------------------------------


def test_architecture_is_marked_derived(architecture):
    assert architecture.derived_from_runs is True


def test_the_submit_form_does_not_ask_for_architecture(architecture):
    """A submitter's answer could contradict the machine's own kernel, and the
    kernel is not the one guessing."""
    form = SubmissionForm(user=User.objects.create_user("sub"))

    assert "cat_architecture" not in form.fields
    assert "propose_architecture" not in form.fields


def test_an_approved_run_binds_the_architecture_it_reported(
    system, reviewer, architecture
):
    submitter = User.objects.create_user("runner")

    _approved_run(submitter, system, reviewer, arch="aarch64")

    bound = ListingCategoryValue.objects.filter(
        listing_system=system, value__category=architecture,
    )
    assert [b.value.value for b in bound] == ["aarch64"]


def test_a_second_run_on_the_same_arch_does_not_duplicate_the_binding(
    system, reviewer, architecture
):
    for name in ("a", "b"):
        _approved_run(
            User.objects.create_user(name), system, reviewer, arch="aarch64",
        )

    assert ListingCategoryValue.objects.filter(
        listing_system=system, value__category=architecture,
    ).count() == 1


def test_a_run_on_another_arch_adds_a_second_binding(
    system, reviewer, architecture
):
    """A listing can legitimately hold two: the same model shipped as both an
    x86_64 and an aarch64 machine is one catalog entry with two proven arches."""
    _approved_run(User.objects.create_user("a"), system, reviewer, arch="aarch64")
    _approved_run(User.objects.create_user("b"), system, reviewer, arch="x86_64")

    bound = ListingCategoryValue.objects.filter(
        listing_system=system, value__category=architecture,
    )
    assert sorted(b.value.value for b in bound) == ["aarch64", "x86_64"]


def test_an_unrecognised_arch_binds_nothing(system, reviewer, architecture):
    """The values are curated, so a kernel reporting something the taxonomy does
    not list is skipped rather than silently adding a value nobody approved."""
    _approved_run(
        User.objects.create_user("a"), system, reviewer, arch="riscv64",
    )

    assert not ListingCategoryValue.objects.filter(
        listing_system=system, value__category=architecture,
    ).exists()
    assert not CategoryValue.objects.filter(value="riscv64").exists()


def test_a_listing_with_no_runs_has_no_architecture(system, architecture):
    """Honest rather than incomplete: nothing has proven what it runs on."""
    assert not ListingCategoryValue.objects.filter(
        listing_system=system, value__category=architecture,
    ).exists()
