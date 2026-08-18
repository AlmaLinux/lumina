"""Revising a software submission a reviewer sent back.

The gap this closes: ``request_changes`` set a submission to needs-changes and
that was the end of the road. ``approve`` accepts a needs-changes submission
(``OPEN_STATUSES`` covers it), so the model always assumed the row would be
revised and looked at again - but nothing let the submitter do the revising.
Their dashboard offered one link, to a listing that is unpublished by definition,
and ``software:detail`` filters on ``published``, so it 404'd.

What is pinned down here:
- the dashboard links a draft to its revise page, never to a 404
- revising edits the draft in place and returns the same submission to pending,
  rather than opening a second one against the same product
- only the submitter revises, and only from needs-changes
- the reviewer's note survives the round trip, so the queue keeps the context
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.urls import reverse

from lumina.core.certification import ValidationLevel
from lumina.releases.models import AlmaLinuxRelease
from lumina.software.models import Software, SoftwareSubmission
from lumina.vendors.models import Vendor

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture(autouse=True)
def releases():
    for major in (8, 9, 10):
        AlmaLinuxRelease.objects.get_or_create(major=major,
                                               defaults={"supported": True})


@pytest.fixture
def vaultwise():
    return Vendor.objects.create(
        name="Vaultwise", slug="vaultwise", scope=Vendor.SCOPE_SOFTWARE,
        verified=True,
    )


@pytest.fixture
def submitter(client):
    user = User.objects.create_user("sub", email="sub@example.com")
    client.force_login(user)
    return user


@pytest.fixture
def reviewer():
    user = User.objects.create_user("rev", email="rev@example.com")
    user.groups.add(Group.objects.get_or_create(name="reviewer")[0])
    return user


@pytest.fixture
def sent_back(client, vaultwise, submitter, reviewer):
    """A submission a reviewer has asked for changes on."""
    response = client.post(reverse("software:submit"), {
        "name": "Vaultwise Archive",
        "vendor": vaultwise.slug,
        "description": "Backs things up.",
        "release_support_9": "on",
        "claimed_validation_level": ValidationLevel.COMMUNITY,
    })
    assert response.status_code == 302, response.context["form"].errors
    submission = SoftwareSubmission.objects.get()
    submission.request_changes(by=reviewer, reason="Add a homepage URL.")
    return submission


def _revise_url(submission):
    return reverse("software:revise", args=[submission.uuid])


# --- the dead end ------------------------------------------------------------


def test_the_dashboard_offers_a_revise_link_and_no_404_link(client, sent_back):
    body = client.get(reverse("accounts:dashboard")).content.decode()

    assert _revise_url(sent_back) in body
    assert "Add a homepage URL." in body
    # The draft is unpublished, so its public URL does not resolve. It must not
    # be offered at all.
    assert sent_back.software.get_absolute_url() not in body


def test_the_public_detail_page_really_does_404_for_a_draft(client, sent_back):
    """Pins the reason the dashboard cannot link there."""
    response = client.get(sent_back.software.get_absolute_url())

    assert response.status_code == 404


def test_one_table_carries_both_the_product_and_its_submission_status(
    client, sent_back
):
    """The two tables were merged: a product and the submission that created it
    are one row, so a status has something to be the status *of*."""
    body = client.get(reverse("accounts:dashboard")).content.decode()

    assert body.count("My software") == 1
    assert "My software submissions" not in body
    assert "Needs changes" in body


# --- revising ---------------------------------------------------------------


def test_the_form_is_prefilled_with_the_draft(client, sent_back):
    body = client.get(_revise_url(sent_back)).content.decode()

    assert "Vaultwise Archive" in body
    assert "Backs things up." in body
    # The reviewer's request is on the page they fix it on.
    assert "Add a homepage URL." in body


def test_revising_updates_the_draft_in_place_and_returns_it_to_pending(
    client, sent_back, vaultwise
):
    response = client.post(_revise_url(sent_back), {
        "name": "Vaultwise Archive",
        "vendor": vaultwise.slug,
        "description": "Backs things up, reliably.",
        "homepage_url": "https://example.com/vaultwise",
        "release_support_9": "on",
        "release_support_10": "on",
        "claimed_validation_level": ValidationLevel.COMMUNITY,
    })

    assert response.status_code == 302
    # One submission, not a second one against the same product.
    assert SoftwareSubmission.objects.count() == 1
    sent_back.refresh_from_db()
    assert sent_back.status == SoftwareSubmission.STATUS_PENDING
    assert sent_back.reviewed_at is None

    product = Software.objects.get(pk=sent_back.software_id)
    assert product.description == "Backs things up, reliably."
    assert product.homepage_url == "https://example.com/vaultwise"
    assert not product.published
    # The newly ticked release is now cited.
    assert sorted(
        r.release.major for r in product.compatibility.all()
    ) == [9, 10]


def test_an_unticked_release_is_dropped_on_revision(client, sent_back, vaultwise):
    """Otherwise "you certified the wrong release" would be unfixable."""
    response = client.post(_revise_url(sent_back), {
        "name": "Vaultwise Archive",
        "vendor": vaultwise.slug,
        "release_support_8": "on",
        "claimed_validation_level": ValidationLevel.COMMUNITY,
    })

    assert response.status_code == 302
    product = Software.objects.get(pk=sent_back.software_id)
    assert [r.release.major for r in product.compatibility.all()] == [8]


def test_the_reviewers_note_survives_so_the_queue_keeps_the_context(
    client, sent_back, vaultwise
):
    client.post(_revise_url(sent_back), {
        "name": "Vaultwise Archive",
        "vendor": vaultwise.slug,
        "release_support_9": "on",
        "claimed_validation_level": ValidationLevel.COMMUNITY,
    })

    sent_back.refresh_from_db()
    assert sent_back.reviewer_notes == "Add a homepage URL."


def test_a_revised_submission_is_back_in_the_review_queue(
    client, sent_back, vaultwise, reviewer
):
    client.post(_revise_url(sent_back), {
        "name": "Vaultwise Archive",
        "vendor": vaultwise.slug,
        "release_support_9": "on",
        "claimed_validation_level": ValidationLevel.COMMUNITY,
    })

    client.force_login(reviewer)
    body = client.get(reverse("review:queue")).content.decode()

    assert "Vaultwise Archive" in body


def test_the_form_still_rejects_a_revision_naming_no_release(
    client, sent_back, vaultwise
):
    """The submit guards apply to a revision too, or a revision is a way around
    them. The release guard is the only
    one, and a certification naming no release certifies nothing."""
    response = client.post(_revise_url(sent_back), {
        "name": "Vaultwise Archive",
        "vendor": vaultwise.slug,
        "claimed_validation_level": ValidationLevel.COMMUNITY,
    })

    assert response.status_code == 200
    sent_back.refresh_from_db()
    assert sent_back.status == SoftwareSubmission.STATUS_NEEDS_CHANGES


# --- who may revise, and when -----------------------------------------------


def test_another_user_cannot_revise_someone_elses_submission(client, sent_back):
    intruder = User.objects.create_user("nosy", email="nosy@example.com")
    client.force_login(intruder)

    assert client.get(_revise_url(sent_back)).status_code == 404


def test_revising_requires_login(client, sent_back):
    client.logout()

    response = client.get(_revise_url(sent_back))

    assert response.status_code == 302
    assert "/login" in response["Location"] or "oidc" in response["Location"]


def test_a_pending_submission_is_not_revisable(client, vaultwise, submitter):
    """Only a submission a reviewer sent back. While it is pending, editing it
    would change what the reviewer is in the middle of reading."""
    client.post(reverse("software:submit"), {
        "name": "Orbital Forge Studio",
        "vendor": vaultwise.slug,
        "release_support_9": "on",
        "claimed_validation_level": ValidationLevel.COMMUNITY,
    })
    pending = SoftwareSubmission.objects.get()

    assert client.get(_revise_url(pending)).status_code == 404


def test_an_approved_submission_is_not_revisable(client, sent_back, reviewer):
    sent_back.approve(by=reviewer, final_level=ValidationLevel.COMMUNITY)

    assert client.get(_revise_url(sent_back)).status_code == 404
