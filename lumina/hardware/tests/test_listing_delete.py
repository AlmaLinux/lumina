"""Deleting a listing, on a database that cannot defer constraint checks.

Reported as a 500 from the admin (Sentry LUMINA-6):

    IntegrityError: CONSTRAINT `listing_version_exactly_one_listing` failed

Django's ``CASCADE`` handler does this, in ``db/models/deletion.py``::

    if field.null and not connections[using].features.can_defer_constraint_checks:
        collector.add_field_update(field, None, sub_objs)

so on MySQL and MariaDB a nullable cascading foreign key is set to NULL *before* its row is
deleted, to break the reference. Every one of this project's "exactly one listing" check
constraints spans exactly such a pair of nullable columns, so that intermediate UPDATE leaves a row
with neither listing set and the constraint refuses it.

SQLite can defer, so it never takes that path - which is why the whole suite passed while deleting
any component or system in production returned a 500.
"""
from __future__ import annotations

import pytest

from lumina.hardware.models import (
    Component,
    ComponentKind,
    ListingCategoryValue,
    ListingVersion,
    System,
)
from lumina.releases.models import AlmaLinuxRelease
from lumina.vendors.models import Vendor

pytestmark = pytest.mark.django_db


@pytest.fixture
def release():
    return AlmaLinuxRelease.objects.get_or_create(major=9, defaults={"supported": True})[0]


def _category_value():
    from lumina.taxonomy.models import Category, CategoryValue

    category = Category.objects.create(name="Delete test category", slug="delete-test-category")
    return CategoryValue.objects.create(category=category, value="Rack", slug="delete-test-rack")


@pytest.fixture
def vendor():
    return Vendor.objects.create(name="Delete Test Vendor", slug="delete-test-vendor")


def test_a_component_with_a_release_row_can_be_deleted(vendor, release):
    component = Component.objects.create(
        vendor=vendor, name="Deletable Card", slug="deletable-card",
        kind=ComponentKind.gpu.value)
    ListingVersion.objects.create(listing_component=component, release=release)

    component.delete()

    assert not Component.objects.filter(pk=component.pk).exists()
    assert not ListingVersion.objects.filter(listing_component__isnull=True,
                                             listing_system__isnull=True).exists()


def test_a_system_with_a_release_row_can_be_deleted(vendor, release):
    """The same pair of columns, the same constraint, the other half of it."""
    system = System.objects.create(vendor=vendor, name="Deletable Box", slug="deletable-box")
    ListingVersion.objects.create(listing_system=system, release=release)

    system.delete()

    assert not System.objects.filter(pk=system.pk).exists()


def test_deleting_a_queryset_of_components_works_too(vendor, release):
    """The admin's bulk action goes through the queryset, which never calls ``Model.delete``."""
    for n in range(2):
        component = Component.objects.create(
            vendor=vendor, name=f"Bulk Card {n}", slug=f"bulk-card-{n}",
            kind=ComponentKind.gpu.value)
        ListingVersion.objects.create(listing_component=component, release=release)

    Component.objects.filter(name__startswith="Bulk Card").delete()

    assert not Component.objects.filter(name__startswith="Bulk Card").exists()


def test_every_two_listing_table_survives_a_delete(vendor, release):
    """Five constraints share this shape, so fixing one table and not the rest would move the 500
    rather than end it."""
    from django.contrib.auth.models import User

    from lumina.hardware.models import Submission

    person = User.objects.create_user("delete-test-person")
    component = Component.objects.create(
        vendor=vendor, name="Thorough Card", slug="thorough-card",
        kind=ComponentKind.gpu.value)
    ListingVersion.objects.create(listing_component=component, release=release)
    Submission.objects.create(listing_component=component, submitter=person)
    ListingCategoryValue.objects.create(
        listing_component=component,
        value=_category_value(),
    )

    component.delete()

    assert not Component.objects.filter(pk=component.pk).exists()
    assert not ListingCategoryValue.objects.filter(listing_component__isnull=True,
                                                   listing_system__isnull=True).exists()


def test_deleting_a_system_unlinks_its_runs_rather_than_deleting_them(vendor, release):
    """``TestRun.listing_system`` is SET_NULL, and a run is evidence somebody submitted. Sweeping
    every nullable relation rather than only the cascading ones would delete it: the fix for one
    constraint would have quietly destroyed the results the catalog is built from."""
    from django.contrib.auth.models import User

    from lumina.results import ingest
    from lumina.results.tests import factories as f

    system = System.objects.create(vendor=vendor, name="Runs Box", slug="runs-box")
    run = ingest.ingest_bundle(
        submitter=User.objects.create_user("delete-run-owner"), source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"],
            results=[f.validate_result("validate.cpu.functional")]))))
    run.listing_system = system
    run.save(update_fields=["listing_system"])

    system.delete()

    run.refresh_from_db()
    assert run.listing_system_id is None, "unlinked"
