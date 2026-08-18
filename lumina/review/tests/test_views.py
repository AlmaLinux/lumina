"""Tests for the review dashboard and submission-action views.

- Queue: lists only pending/needs-changes submissions; 403 for non-reviewers.
- Submission detail page is permission-gated and shows the listing, any
  pending proposed CategoryValues attached to the submission, and action
  buttons.
- POST /review/<pk>/approve writes an audit log entry and calls
  ``Submission.approve`` with the reviewer's ``final_level`` choice.
- POST /review/<pk>/reject records the reason and writes an audit entry.
- POST /review/<pk>/promote-value/<value_pk> approves the taxonomy value
  and writes an audit entry.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.urls import reverse

from lumina.audit.models import AuditLogEntry
from lumina.core.certification import ValidationLevel
from lumina.hardware.models import Submission, System
from lumina.taxonomy.models import Category, CategoryValue
from lumina.vendors.models import Vendor

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture
def reviewer(client):
    u = User.objects.create_user(username="rev")
    u.groups.add(Group.objects.create(name="reviewer"))
    client.force_login(u)
    return u


@pytest.fixture
def plain(client):
    u = User.objects.create_user(username="plain")
    client.force_login(u)
    return u


@pytest.fixture
def vendor():
    return Vendor.objects.create(name="Dell")


@pytest.fixture
def submitter():
    return User.objects.create_user(username="submitter")


@pytest.fixture
def pending_submission(vendor, submitter):
    system = System.objects.create(
        name="PowerEdge R750", vendor=vendor, model_number="R750", created_by=submitter
    )
    # Cites a release, because validation is per release: approving a submission
    # records the reviewer's tier against each release the listing names, and one
    # that names none publishes without certifying anything. The submit form always
    # produces at least one, so this matches what a reviewer actually sees.
    from lumina.hardware.models import ListingVersion
    from lumina.releases.models import AlmaLinuxRelease

    ListingVersion.objects.create(
        listing_system=system,
        release=AlmaLinuxRelease.objects.get_or_create(
            major=9, defaults={"supported": True},
        )[0],
        source=ListingVersion.SOURCE_DECLARED,
    )
    submission = Submission.objects.create(
        submitter=submitter,
        listing_system=system,
        claimed_validation_level=ValidationLevel.COMMUNITY,
    )
    # The claim is recorded on the submission, not re-read off the listing at approval
    # time; see ``Submission.cited_releases``. The submit form sets this from the
    # release rows it just wrote, which for a fresh listing is all of them.
    submission.cited_releases.set(
        AlmaLinuxRelease.objects.filter(major=9),
    )
    return submission


class QueueViewTests:
    def test_reviewer_sees_pending(self, client, reviewer, pending_submission):
        resp = client.get(reverse("review:queue"))
        assert resp.status_code == 200
        # Assert on rendered HTML rather than resp.context to sidestep a
        # known Django 5.1 / Python 3.14 test-client context-copy bug.
        assert b"PowerEdge R750" in resp.content

    def test_approved_not_in_queue(self, client, reviewer, pending_submission):
        pending_submission.approve(by=reviewer, final_level=ValidationLevel.COMMUNITY)
        resp = client.get(reverse("review:queue"))
        assert b"PowerEdge R750" not in resp.content

    def test_non_reviewer_forbidden(self, client, plain, pending_submission):
        resp = client.get(reverse("review:queue"))
        assert resp.status_code == 403


class ApproveActionTests:
    def test_approve_publishes_and_logs(self, client, reviewer, pending_submission):
        url = reverse("review:approve", args=[pending_submission.pk])
        resp = client.post(url, {"final_level": ValidationLevel.COMMUNITY})
        assert resp.status_code == 302

        pending_submission.refresh_from_db()
        assert pending_submission.status == Submission.STATUS_APPROVED
        assert pending_submission.listing.published is True

        entry = AuditLogEntry.objects.filter(action="submission.approve").first()
        assert entry is not None
        assert entry.actor == reviewer
        assert entry.target_id == pending_submission.pk

    def test_a_posted_final_level_cannot_exceed_the_manual_ceiling(
        self, client, reviewer, pending_submission
    ):
        """This test used to assert the escalation as a feature.

        As ``test_approve_respects_final_level_override`` it posted
        ``final_level=almalinux`` and asserted the listing came out
        AlmaLinux-certified, and it passed, because the view's only check was that the
        string was one of the three enum values. Enum membership is not a permission
        check: the submitter here has no vendor membership, no staff flag, and no
        evidence attached, and the Foundation's own certification was one POST field
        away. Its docstring at ``test_submit_flow.py`` even claimed the submitter's
        entitlement was enforced on this path. It was not, on either side.
        """
        url = reverse("review:approve", args=[pending_submission.pk])

        resp = client.post(url, {"final_level": ValidationLevel.ALMALINUX})

        assert resp.status_code == 302
        pending_submission.refresh_from_db()
        assert pending_submission.status == Submission.STATUS_APPROVED
        assert pending_submission.listing.validation_level == ValidationLevel.COMMUNITY

    def test_an_empty_post_body_does_not_grant_the_submitters_claim(
        self, client, reviewer, pending_submission, submitter, vendor
    ):
        """With ``final_level`` absent the view falls back to the submission's own
        ``claimed_validation_level``, so a submitter who claimed vendor was granted it
        by a reviewer who chose nothing at all."""
        from lumina.vendors.models import VendorMembership

        VendorMembership.objects.create(
            user=submitter, vendor=vendor, role=VendorMembership.ROLE_SUBMITTER,
        )
        pending_submission.on_behalf_of = vendor
        pending_submission.claimed_validation_level = ValidationLevel.VENDOR
        pending_submission.save(
            update_fields=["on_behalf_of", "claimed_validation_level"]
        )

        client.post(reverse("review:approve", args=[pending_submission.pk]), {})

        pending_submission.refresh_from_db()
        assert pending_submission.listing.validation_level == ValidationLevel.COMMUNITY

    def test_the_reviewer_is_not_offered_a_tier_it_cannot_award(
        self, client, reviewer, pending_submission
    ):
        """The cap is on the model, so the page showing three options was not a
        vulnerability by itself - just a reviewer being offered two choices the model
        would silently refuse to honour."""
        body = client.get(
            reverse("review:detail", args=[pending_submission.pk])
        ).content.decode()

        assert 'value="vendor"' not in body
        assert 'value="almalinux"' not in body
        assert 'name="final_level"' in body

    def test_non_reviewer_cannot_approve(self, client, plain, pending_submission):
        url = reverse("review:approve", args=[pending_submission.pk])
        resp = client.post(url, {"final_level": ValidationLevel.COMMUNITY})
        assert resp.status_code == 403
        pending_submission.refresh_from_db()
        assert pending_submission.status == Submission.STATUS_PENDING


class RejectActionTests:
    def test_reject_records_reason_and_logs(self, client, reviewer, pending_submission):
        url = reverse("review:reject", args=[pending_submission.pk])
        resp = client.post(url, {"reason": "missing kernel version"})
        assert resp.status_code == 302
        pending_submission.refresh_from_db()
        assert pending_submission.status == Submission.STATUS_REJECTED
        assert "missing kernel version" in pending_submission.reviewer_notes
        assert AuditLogEntry.objects.filter(action="submission.reject").exists()


class PromoteValueActionTests:
    def test_promote_approves_value_and_logs(self, client, reviewer, submitter):
        cat = Category.objects.create(name="Architecture", slug="arch")
        val = CategoryValue.propose(category=cat, value="riscv64", proposed_by=submitter)

        url = reverse("review:promote_value", args=[val.pk])
        resp = client.post(url)
        assert resp.status_code == 302
        val.refresh_from_db()
        assert val.status == CategoryValue.STATUS_APPROVED
        assert val.approved_by == reviewer
        assert AuditLogEntry.objects.filter(action="taxonomy.value.approve").exists()

    def test_non_reviewer_cannot_promote(self, client, plain, submitter):
        cat = Category.objects.create(name="Architecture", slug="arch")
        val = CategoryValue.propose(category=cat, value="riscv64", proposed_by=submitter)
        resp = client.post(reverse("review:promote_value", args=[val.pk]))
        assert resp.status_code == 403
        val.refresh_from_db()
        assert val.status == CategoryValue.STATUS_PENDING
