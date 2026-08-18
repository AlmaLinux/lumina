"""How software categories are shaped in the shared taxonomy.

Software has exactly **one** category, "Category", whose values are Backup, AI,
Storage, and the rest. The alternative - one `Category` row per name, each holding
a single value equal to its own name - renders as ten sidebar cards whose headers
and checkboxes say the same word twice, and is what these tests exist to prevent.

The second rule is a containment rule: the software seed must never reach a
category it does not own. Software wants a "Storage" facet and hardware already
has a Storage category (SATA / SAS / NVMe); keying the software seed on a
name-derived slug silently repurposed hardware's row.
"""
from __future__ import annotations

import pytest
from django.db.models import F
from django.utils.html import escape

from lumina.core.management.commands.seed_devstack import (
    _SAMPLE_SOFTWARE_CATEGORIES,
    _SOFTWARE_CATEGORY_NAME,
    _SOFTWARE_CATEGORY_SLUG,
    Command,
)
from lumina.taxonomy.models import Category, CategoryValue

pytestmark = pytest.mark.django_db


def _seed() -> None:
    """Just the software-category step, so the test needs no network.

    The full ``seed_devstack`` run fetches vendor logos over HTTP.
    """
    Command()._seed_software_categories()


def test_software_seeds_exactly_one_category():
    _seed()

    categories = Category.objects.filter(applies_to=Category.APPLIES_SOFTWARE)

    assert [c.name for c in categories] == [_SOFTWARE_CATEGORY_NAME]


def test_the_ten_names_are_values_of_that_category_not_categories():
    _seed()

    category = Category.objects.get(slug=_SOFTWARE_CATEGORY_SLUG)

    assert sorted(v.value for v in category.values.all()) == sorted(
        _SAMPLE_SOFTWARE_CATEGORIES
    )
    # The failure this pins down: a value equal to its own category's name, which
    # is what produced a card headed "Backup" containing one checkbox labelled
    # "Backup".
    assert not CategoryValue.objects.filter(value=F("category__name")).exists()


def test_seeding_does_not_touch_a_hardware_category_of_the_same_name():
    """The collision that shipped: hardware's Storage became software-only.

    Software's own list contains "Storage", and hardware already owns a Storage
    category. Seeding must leave that row's scope and values exactly as found.
    """
    hardware_storage = Category.objects.create(
        name="Storage", slug="storage", applies_to=Category.APPLIES_BOTH,
    )
    for value in ("SATA", "SAS", "NVMe"):
        CategoryValue.objects.create(category=hardware_storage, value=value)

    _seed()

    hardware_storage.refresh_from_db()
    assert hardware_storage.applies_to == Category.APPLIES_BOTH
    assert sorted(v.value for v in hardware_storage.values.all()) == [
        "NVMe", "SAS", "SATA",
    ]


def test_seeding_twice_changes_nothing():
    _seed()
    _seed()

    assert Category.objects.filter(
        applies_to=Category.APPLIES_SOFTWARE
    ).count() == 1
    assert CategoryValue.objects.filter(
        category__slug=_SOFTWARE_CATEGORY_SLUG
    ).count() == len(_SAMPLE_SOFTWARE_CATEGORIES)


def test_the_browse_panel_renders_one_category_card(client):
    """End of the chain: one sidebar card, ten checkboxes inside it."""
    _seed()

    response = client.get("/software/")

    assert response.status_code == 200
    rendered = response.content.decode()
    assert rendered.count(f'data-category="{_SOFTWARE_CATEGORY_SLUG}"') == 1
    for name in _SAMPLE_SOFTWARE_CATEGORIES:
        # escape(), because "Cloud & Virtualization" reaches the page as
        # "Cloud &amp; Virtualization".
        assert escape(name) in rendered
