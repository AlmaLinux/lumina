"""The reviewer side: the software tab and its three decisions.

One tab, three tables - submissions, edit proposals, and community-reported
majors - because the queue is already five tabs deep and all three are software
decisions.

The reported-major table is the smallest decision in the queue: product, release,
who reported it, approve or reject. Rejecting deletes the row so the same major
can be reported again later.

All endpoints catch the state machine's ValueError. Hardware's equivalents do not,
which makes a double-approve a 500 there.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.urls import reverse

from lumina.core.certification import ValidationLevel
from lumina.releases.models import AlmaLinuxRelease
from lumina.software import services
from lumina.software.models import (
    Software,
    SoftwareCompatibility,
    SoftwareEditProposal,
    SoftwareSubmission,
)
from lumina.vendors.models import Vendor

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture(autouse=True)
def releases():
    for major in (8, 9, 10, 11):
        AlmaLinuxRelease.objects.get_or_create(major=major,
                                               defaults={"supported": True})


@pytest.fixture
def reviewer(client):
    user = User.objects.create_user("rev", email="rev@example.com")
    user.groups.add(Group.objects.get_or_create(name="reviewer")[0])
    client.force_login(user)
    return user


@pytest.fixture
def submitter():
    return User.objects.create_user("sub", email="sub@example.com")


@pytest.fixture
def product(submitter):
    vendor = Vendor.objects.create(name="Vaultwise", scope=Vendor.SCOPE_SOFTWARE,
                                   verified=True)
    software = Software.objects.create(
        vendor=vendor, name="Vaultwise Archive", published=False,
        owner_vendor=vendor,
    )
    SoftwareCompatibility.objects.create(
        software=software, release=AlmaLinuxRelease.objects.get(major=9),
    )
    return software


@pytest.fixture
def submission(product, submitter):
    return SoftwareSubmission.objects.create(
        submitter=submitter, software=product,
        claimed_validation_level=ValidationLevel.COMMUNITY,
    )


# --- the queue ----------------------------------------------------------------


def test_the_queue_has_a_software_tab_listing_pending_submissions(
    client, reviewer, submission
):
    body = client.get(reverse("review:queue")).content.decode()

    assert "tab-software" in body
    assert "Vaultwise Archive" in body


def test_a_reported_major_appears_in_the_software_tab(client, reviewer, product,
                                                      submitter):
    product.published = True
    product.save(update_fields=["published"])
    services.report_new_major(
        software=product, release=AlmaLinuxRelease.objects.get(major=11),
        user=submitter,
    )

    body = client.get(reverse("review:queue")).content.decode()

    assert "AlmaLinux 11" in body
    assert "reported" in body.lower()


def test_a_software_edit_proposal_appears_in_the_software_tab(client, reviewer,
                                                              product, submitter):
    SoftwareEditProposal.objects.create(
        software=product, proposed_by=submitter, name="Renamed",
    )

    body = client.get(reverse("review:queue")).content.decode()

    assert "Renamed" in body or "Vaultwise Archive" in body


# --- approving a submission ---------------------------------------------------


def test_approving_publishes_the_listing(client, reviewer, submission):
    client.post(reverse("review:software_approve", args=[submission.pk]),
                {"final_level": ValidationLevel.COMMUNITY}, follow=True)

    submission.refresh_from_db()
    assert submission.status == SoftwareSubmission.STATUS_APPROVED
    assert Software.objects.get(pk=submission.software_id).published is True


def test_approving_at_vendor_level_certifies_the_cited_majors(client, reviewer,
                                                              submission):
    client.post(reverse("review:software_approve", args=[submission.pk]),
                {"final_level": ValidationLevel.VENDOR}, follow=True)

    row = submission.software.compatibility.get()
    assert row.validation_level == ValidationLevel.VENDOR


def test_an_invalid_final_level_is_a_400(client, reviewer, submission):
    resp = client.post(reverse("review:software_approve", args=[submission.pk]),
                       {"final_level": "platinum"})

    assert resp.status_code == 400
    submission.refresh_from_db()
    assert submission.status == SoftwareSubmission.STATUS_PENDING


def test_a_double_approve_is_a_message_not_a_500(client, reviewer, submission):
    url = reverse("review:software_approve", args=[submission.pk])
    client.post(url, {"final_level": ValidationLevel.COMMUNITY})

    resp = client.post(url, {"final_level": ValidationLevel.COMMUNITY}, follow=True)

    assert resp.status_code == 200


def test_rejecting_records_the_reason(client, reviewer, submission):
    client.post(reverse("review:software_reject", args=[submission.pk]),
                {"reason": "Needs a support URL."}, follow=True)

    submission.refresh_from_db()
    assert submission.status == SoftwareSubmission.STATUS_REJECTED
    assert "support URL" in submission.reviewer_notes
    assert Software.objects.get(pk=submission.software_id).published is False


def test_requesting_changes_sends_it_back(client, reviewer, submission):
    client.post(reverse("review:software_request_changes", args=[submission.pk]),
                {"reason": "Clarify the licensing."}, follow=True)

    submission.refresh_from_db()
    assert submission.status == SoftwareSubmission.STATUS_NEEDS_CHANGES


# --- reported majors ----------------------------------------------------------


def test_approving_a_reported_major_publishes_it(client, reviewer, product, submitter):
    product.published = True
    product.save(update_fields=["published"])
    row = services.report_new_major(
        software=product, release=AlmaLinuxRelease.objects.get(major=11),
        user=submitter,
    )

    client.post(reverse("review:software_major_approve", args=[row.pk]), follow=True)

    row.refresh_from_db()
    assert row.status == SoftwareCompatibility.STATUS_APPROVED
    assert row.validation_level == ValidationLevel.COMMUNITY


def test_rejecting_a_reported_major_deletes_the_row(client, reviewer, product,
                                                    submitter):
    """So the same major can be reported again once it really does work."""
    product.published = True
    product.save(update_fields=["published"])
    row = services.report_new_major(
        software=product, release=AlmaLinuxRelease.objects.get(major=11),
        user=submitter,
    )

    client.post(reverse("review:software_major_reject", args=[row.pk]),
                {"reason": "Unverified."}, follow=True)

    assert not SoftwareCompatibility.objects.filter(pk=row.pk).exists()
    assert not product.compatibility.filter(release__major=11).exists()


# --- edit proposals -----------------------------------------------------------


def test_approving_an_edit_proposal_copies_only_the_filled_fields(
    client, reviewer, product, submitter
):
    original_description = product.description
    proposal = SoftwareEditProposal.objects.create(
        software=product, proposed_by=submitter, name="Vaultwise Archive Pro",
    )

    client.post(reverse("review:software_edit_approve", args=[proposal.pk]),
                follow=True)

    product.refresh_from_db()
    assert product.name == "Vaultwise Archive Pro"
    assert product.description == original_description


def test_rejecting_an_edit_proposal_leaves_the_listing_alone(client, reviewer,
                                                             product, submitter):
    proposal = SoftwareEditProposal.objects.create(
        software=product, proposed_by=submitter, name="Nope",
    )

    client.post(reverse("review:software_edit_reject", args=[proposal.pk]),
                {"reason": "Not the product's name."}, follow=True)

    product.refresh_from_db()
    proposal.refresh_from_db()
    assert product.name == "Vaultwise Archive"
    assert proposal.status == SoftwareEditProposal.STATUS_REJECTED


# --- permissions --------------------------------------------------------------


def test_a_non_reviewer_cannot_act_on_a_submission(client, submission, submitter):
    client.force_login(submitter)

    resp = client.post(reverse("review:software_approve", args=[submission.pk]),
                       {"final_level": ValidationLevel.COMMUNITY})

    assert resp.status_code == 403
    submission.refresh_from_db()
    assert submission.status == SoftwareSubmission.STATUS_PENDING
