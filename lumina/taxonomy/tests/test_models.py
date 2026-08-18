"""Tests for taxonomy models.

These tests define the intended behavior of Category and CategoryValue:

- Categories are admin-curated and scoped to system/component/both.
- CategoryValues default to approved for admin-created values, but any value
  proposed via a submission must enter in status=pending and be invisible to
  public filter listings until approved.
- Approving a pending value promotes it globally (status=approved, records
  approver and timestamp).
- Rejecting a pending value keeps the historical record but excludes it from
  listings.
- ``collapsed_limit`` is per-category, admin-configurable, defaulting to
  settings.LUMINA_DEFAULT_COLLAPSED_LIMIT.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.db.utils import IntegrityError
from django.utils import timezone

from lumina.taxonomy.models import Category, CategoryValue

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture
def admin_user():
    return User.objects.create_user(username="admin", is_staff=True)


@pytest.fixture
def proposer():
    return User.objects.create_user(username="proposer")


@pytest.fixture
def arch_category():
    return Category.objects.create(
        name="Architecture",
        slug="architecture",
        applies_to=Category.APPLIES_BOTH,
    )


class CategoryTests:
    def test_slug_unique(self, arch_category):
        with pytest.raises(IntegrityError):
            Category.objects.create(name="Arch2", slug="architecture")


class CategoryValueCreationTests:
    def test_admin_created_values_are_approved(self, arch_category):
        val = CategoryValue.objects.create(
            category=arch_category, value="x86_64"
        )
        assert val.status == CategoryValue.STATUS_APPROVED
        assert val.slug == "x86_64"

    def test_propose_creates_pending_value(self, arch_category, proposer):
        val = CategoryValue.propose(
            category=arch_category,
            value="riscv64",
            proposed_by=proposer,
        )
        assert val.status == CategoryValue.STATUS_PENDING
        assert val.proposed_by == proposer
        assert val.approved_at is None
        assert val.approved_by is None

    def test_proposed_value_hidden_from_approved_queryset(self, arch_category, proposer):
        CategoryValue.propose(
            category=arch_category, value="riscv64", proposed_by=proposer
        )
        assert CategoryValue.objects.approved().count() == 0

    def test_duplicate_value_within_category_rejected(self, arch_category):
        CategoryValue.objects.create(category=arch_category, value="x86_64")
        with pytest.raises(IntegrityError):
            CategoryValue.objects.create(category=arch_category, value="x86_64")

    def test_same_value_allowed_in_different_categories(self, arch_category):
        net = Category.objects.create(name="Network", slug="network")
        CategoryValue.objects.create(category=arch_category, value="generic")
        # Should not raise.
        CategoryValue.objects.create(category=net, value="generic")


class CategoryValuePromotionTests:
    def test_approve_promotes_pending_value(self, arch_category, proposer, admin_user):
        val = CategoryValue.propose(
            category=arch_category, value="riscv64", proposed_by=proposer
        )
        before = timezone.now()
        val.approve(by=admin_user)
        assert val.status == CategoryValue.STATUS_APPROVED
        assert val.approved_by == admin_user
        assert val.approved_at is not None
        assert val.approved_at >= before

    def test_approved_value_appears_in_approved_queryset(
        self, arch_category, proposer, admin_user
    ):
        val = CategoryValue.propose(
            category=arch_category, value="riscv64", proposed_by=proposer
        )
        val.approve(by=admin_user)
        assert val in CategoryValue.objects.approved()

    def test_reject_marks_rejected_and_excludes(
        self, arch_category, proposer, admin_user
    ):
        val = CategoryValue.propose(
            category=arch_category, value="bogus", proposed_by=proposer
        )
        val.reject(by=admin_user)
        assert val.status == CategoryValue.STATUS_REJECTED
        assert val not in CategoryValue.objects.approved()

    def test_cannot_approve_already_approved(self, arch_category, admin_user):
        val = CategoryValue.objects.create(category=arch_category, value="x86_64")
        with pytest.raises(ValueError):
            val.approve(by=admin_user)

    def test_cannot_reject_already_rejected(self, arch_category, proposer, admin_user):
        val = CategoryValue.propose(
            category=arch_category, value="bogus", proposed_by=proposer
        )
        val.reject(by=admin_user)
        with pytest.raises(ValueError):
            val.reject(by=admin_user)
