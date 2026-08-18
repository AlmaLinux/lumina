"""Submitting software for certification, and what approving one does.

One guard the form owns rather than the database: at least one AlmaLinux major,
because a certification naming no release certifies nothing. (Licensing was the
second, and the concept is gone - see docs/api.md on why the catalog does not
record it.)

The cited majors are stored by ``form.save()``, not at approval. The submission
row carries no list of them, so the draft listing is where the reviewer reads them
from - the same arrangement as hardware's ``_attach_release_versions``.

Inline vendor creation grants submit rights only, never ownership, so the real
vendor still has something to claim.
"""
from __future__ import annotations

import re

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.urls import reverse

from lumina.core.certification import ValidationLevel
from lumina.releases.models import AlmaLinuxRelease
from lumina.software.models import (
    Software,
    SoftwareCertification,
    SoftwareSubmission,
)
from lumina.taxonomy.models import Category, CategoryValue
from lumina.vendors.models import Vendor, VendorMembership

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture(autouse=True)
def releases():
    for major in (8, 9, 10):
        AlmaLinuxRelease.objects.get_or_create(major=major,
                                               defaults={"supported": True})


@pytest.fixture(autouse=True)
def backup_category():
    category = Category.objects.create(
        name="Backup", slug="backup", applies_to=Category.APPLIES_SOFTWARE,
        allow_suggestions=True,
    )
    return CategoryValue.objects.create(category=category, value="Backup")


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
def reviewer(client):
    user = User.objects.create_user("rev", email="rev@example.com")
    user.groups.add(Group.objects.get_or_create(name="reviewer")[0])
    return user


def payload(vendor=None, **overrides):
    """``vendor`` accepts a Vendor, a slug string, or the inline sentinel."""
    data = {
        "name": "Vaultwise Archive",
        "vendor": getattr(vendor, "slug", vendor) or "",
        "description": "Backs things up.",
        "homepage_url": "https://example.com/vaultwise",
        "release_support_9": "on",
        "claimed_validation_level": ValidationLevel.COMMUNITY,
    }
    data.update(overrides)
    return {k: v for k, v in data.items() if v not in (None, "")}


def test_the_category_options_render_as_real_checkboxes(client, submitter):
    """They were rendering as full-width pills with nothing to click.

    Two causes, both fixed: the widget class fell through to ``form-control``, and
    the template printed the bound field bare, so Django's default markup gave no
    ``.form-check`` wrapper to align an input against its label.
    """
    body = client.get(reverse("software:submit")).content.decode()

    tags = re.findall(r'<input[^>]*name="cat_backup"[^>]*>', body)
    assert tags, "the category field did not render"
    for tag in tags:
        assert 'type="checkbox"' in tag
        assert "form-check-input" in tag
        assert "form-control" not in tag
    # Each option sits in the same wrapper the release block above it uses.
    assert '<span class="form-check-label">Backup</span>' in body


# --- the form-level guard -----------------------------------------------------


def test_a_submission_citing_no_release_is_rejected(client, vaultwise, submitter):
    data = payload(vaultwise)
    data.pop("release_support_9")

    resp = client.post(reverse("software:submit"), data)

    assert resp.status_code == 200
    assert Software.objects.count() == 0
    assert b"almalinux" in resp.content.lower()


# --- what save() writes -------------------------------------------------------


def test_submitting_creates_an_unpublished_listing_and_a_pending_submission(
    client, vaultwise, submitter
):
    client.post(reverse("software:submit"), payload(vaultwise))

    product = Software.objects.get()
    assert product.published is False
    assert SoftwareSubmission.objects.get().status == SoftwareSubmission.STATUS_PENDING


def test_the_cited_majors_are_stored_at_submit_time(client, vaultwise, submitter):
    """The submission row has no list of majors, so the draft listing is where a
    reviewer reads them from."""
    client.post(reverse("software:submit"),
                payload(vaultwise, release_support_9="on", release_support_10="on"))

    majors = set(
        Software.objects.get().compatibility.values_list("release__major", flat=True)
    )
    assert majors == {9, 10}


def test_cited_majors_are_invisible_publicly_while_the_listing_is_unpublished(
    client, vaultwise, submitter
):
    client.post(reverse("software:submit"), payload(vaultwise))
    product = Software.objects.get()

    assert client.get(product.get_absolute_url()).status_code == 404


def test_a_chosen_category_is_bound(client, vaultwise, submitter, backup_category):
    client.post(reverse("software:submit"),
                payload(vaultwise, cat_backup=[backup_category.slug]))

    product = Software.objects.get()
    assert product.category_values.get().value == backup_category


# --- inline vendor ------------------------------------------------------------


def test_an_inline_vendor_is_software_scoped_and_leaves_ownership_vacant(
    client, submitter
):
    client.post(reverse("software:submit"), payload(
        vendor="__new__", new_vendor_name="Orbital Forge",
    ))

    vendor = Vendor.objects.get(name="Orbital Forge")
    assert vendor.scope == Vendor.SCOPE_SOFTWARE
    assert vendor.published is False
    assert vendor.is_claimed is False
    assert VendorMembership.objects.get(
        user=submitter, vendor=vendor
    ).role == VendorMembership.ROLE_SUBMITTER


def test_an_inline_vendor_submitter_can_only_claim_the_community_tier(
    client, submitter
):
    """An inline-proposed vendor is unverified by definition, so it confers nothing.

    The submission is now accepted at the community tier rather than rejected: the
    level field is not offered to someone with no standing, so a posted vendor
    claim means a tampered or stale form, and discarding the whole submission over
    a control they were never shown is the wrong trade.
    """
    resp = client.post(reverse("software:submit"), payload(
        vendor="__new__", new_vendor_name="Orbital Forge",
        claimed_validation_level=ValidationLevel.VENDOR,
    ))

    assert resp.status_code == 302
    submission = SoftwareSubmission.objects.get()
    assert submission.claimed_validation_level == ValidationLevel.COMMUNITY


# --- approving ----------------------------------------------------------------


def test_approving_at_vendor_level_certifies_every_cited_major(
    client, vaultwise, submitter, reviewer
):
    VendorMembership.objects.create(
        user=submitter, vendor=vaultwise, role=VendorMembership.ROLE_SUBMITTER,
    )
    client.post(reverse("software:submit"), payload(
        vaultwise, release_support_9="on", release_support_10="on",
        on_behalf_of=vaultwise.slug,
        claimed_validation_level=ValidationLevel.VENDOR,
    ))
    submission = SoftwareSubmission.objects.get()

    submission.approve(by=reviewer, final_level=ValidationLevel.VENDOR)

    product = Software.objects.get()
    assert product.published is True
    assert product.validation_level == ValidationLevel.VENDOR
    assert SoftwareCertification.objects.filter(level=ValidationLevel.VENDOR).count() == 2


def test_approving_at_community_level_records_the_submitters_own_confirmation(
    client, vaultwise, submitter, reviewer
):
    """Otherwise a community listing publishes with a count of zero, which reads
    as nobody having said it works."""
    client.post(reverse("software:submit"), payload(vaultwise))
    submission = SoftwareSubmission.objects.get()

    submission.approve(by=reviewer, final_level=ValidationLevel.COMMUNITY)

    row = Software.objects.get().compatibility.get()
    assert row.validation_level == ValidationLevel.COMMUNITY
    assert row.attestations.get().user == submitter


def test_approving_publishes_an_inline_proposed_vendor(client, submitter, reviewer):
    client.post(reverse("software:submit"), payload(
        vendor="__new__", new_vendor_name="Orbital Forge",
    ))
    submission = SoftwareSubmission.objects.get()

    submission.approve(by=reviewer, final_level=ValidationLevel.COMMUNITY)

    assert Vendor.objects.get(name="Orbital Forge").published is True


def test_a_revalidation_activates_a_pending_community_reported_major(
    client, vaultwise, submitter, reviewer
):
    """The collision case: a community member reported AlmaLinux 10 and the vendor
    then cites it. The pending row must flip rather than hit
    unique(software, release)."""
    from lumina.software import services

    client.post(reverse("software:submit"), payload(vaultwise))
    first = SoftwareSubmission.objects.get()
    first.approve(by=reviewer, final_level=ValidationLevel.COMMUNITY)
    product = Software.objects.get()
    reporter = User.objects.create_user("rep", email="rep@example.com")
    services.report_new_major(
        software=product, release=AlmaLinuxRelease.objects.get(major=10), user=reporter,
    )

    client.post(reverse("software:submit"), payload(
        vaultwise, software_slug=product.slug,
        release_support_9="on", release_support_10="on",
    ))
    SoftwareSubmission.objects.exclude(pk=first.pk).get().approve(
        by=reviewer, final_level=ValidationLevel.COMMUNITY
    )

    assert Software.objects.count() == 1
    assert product.compatibility.approved().count() == 2
    assert product.compatibility.pending().count() == 0


def test_a_double_approve_is_refused(client, vaultwise, submitter, reviewer):
    client.post(reverse("software:submit"), payload(vaultwise))
    submission = SoftwareSubmission.objects.get()
    submission.approve(by=reviewer, final_level=ValidationLevel.COMMUNITY)

    with pytest.raises(ValueError):
        submission.approve(by=reviewer, final_level=ValidationLevel.COMMUNITY)
