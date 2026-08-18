"""Tests for the end-user submission flow.

Behavior pinned down:

- GET /submit/ requires authentication (302 to login for anonymous).
- POST creates a *new* System listing in draft state along with a pending
  Submission; the submitter is recorded, vendor FK resolves from the
  posted vendor slug, and claimed_validation_level is validated against
  ``derive_allowed_levels`` (plain users can only claim USER).
- Claiming a level the user is not eligible for returns a form error;
  nothing is persisted.
- Uploaded test-result files attach to the created Submission.
- The submitter may tag the listing with existing approved CategoryValues
  and/or propose new ones; proposed values land in status=pending.
- The form cannot target an existing listing at all: see
  ``NoRevalidationThroughThisFormTests`` for what that used to allow.
- An email notification is sent to ``settings.LUMINA_REVIEW_NOTIFY_EMAILS``
  on submission so reviewers know work is waiting.

The line this replaces described re-validation as "POST to /submit/?listing=<slug>".
No such query parameter ever existed; the field was posted in the body.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from lumina.core.certification import ValidationLevel
from lumina.hardware.models import Submission, System
from lumina.taxonomy.models import Category, CategoryValue
from lumina.vendors.models import Vendor, VendorMembership

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture
def submitter(client):
    u = User.objects.create_user(username="alice", email="alice@example.com")
    client.force_login(u)
    return u


@pytest.fixture
def dell():
    return Vendor.objects.create(name="Dell", verified=True)


@pytest.fixture(autouse=True)
def _notify_emails(settings):
    settings.LUMINA_REVIEW_NOTIFY_EMAILS = ["reviewers@example.com"]


class SubmitGetTests:
    def test_anonymous_redirected_to_login(self, client):
        resp = client.get(reverse("submit:start"))
        assert resp.status_code in (302, 401)

    def test_authenticated_gets_form(self, client, submitter, dell):
        resp = client.get(reverse("submit:start"))
        assert resp.status_code == 200
        assert b"<form" in resp.content


class CreateNewListingTests:
    def _payload(self, dell: Vendor, **overrides) -> dict:
        data = {
            "kind": "system",
            "name": "PowerEdge R750",
            "model_number": "R750",
            "vendor": dell.slug,
            "description": "2U rack server",
            "claimed_validation_level": ValidationLevel.COMMUNITY,
            "submitter_notes": "Tested on 5.14 kernel.",
        }
        data.update(overrides)
        return data

    def test_happy_path_creates_draft_listing_and_submission(
        self, client, submitter, dell
    ):
        resp = client.post(reverse("submit:start"), self._payload(dell))
        assert resp.status_code == 302

        system = System.objects.get(name="PowerEdge R750")
        assert system.published is False
        assert system.created_by == submitter

        submission = Submission.objects.get()
        assert submission.submitter == submitter
        assert submission.listing_system == system
        assert submission.status == Submission.STATUS_PENDING

    def test_file_upload_attaches_to_submission(self, client, submitter, dell):
        data = self._payload(dell)
        data["attachments"] = SimpleUploadedFile(
            "results.log", b"test output", content_type="text/plain"
        )
        client.post(reverse("submit:start"), data)
        submission = Submission.objects.get()
        assert submission.attachments.count() == 1
        assert submission.attachments.first().file.name.endswith("results.log")

    def test_an_attachment_records_its_digest(self, client, submitter, dell):
        """``TestResultAttachment.sha256`` had no writer anywhere in the project.

        The column existed from the start, the create call passed ``submission`` and
        ``file`` and nothing else, the admin listed it readonly, and approval did not
        backfill it, so every attachment ever uploaded stored an empty string. This is
        not manifest verification and does not pretend to be - nothing here checks the
        file against a declared digest, because a declared submission has no manifest
        to check against. It makes the bytes a reviewer looked at identifiable
        afterwards, which is the floor.
        """
        import hashlib

        content = b"test output"
        data = self._payload(dell)
        data["attachments"] = SimpleUploadedFile(
            "results.log", content, content_type="text/plain",
        )

        client.post(reverse("submit:start"), data)

        attachment = Submission.objects.get().attachments.get()
        assert attachment.sha256 == hashlib.sha256(content).hexdigest()

    def test_the_stored_file_is_not_truncated_by_hashing_it(
        self, client, submitter, dell
    ):
        """The stored bytes are the whole upload, not just what hashing left behind.

        Hashing consumes the stream, and the digest and the saved file come from one
        read of it. This passes with or without ``hash_upload``'s trailing seek, because
        Django's ``File.chunks()`` seeks to 0 itself - checked by removing that seek and
        watching this stay green, so it is stated here rather than implied. What it
        pins is the property, not the mechanism: a save path that starts reading from
        wherever the last reader stopped writes a truncated file whose digest still
        looks right, and larger than one chunk so it cannot pass by being small.
        """
        content = b"a" * 5000
        data = self._payload(dell)
        data["attachments"] = SimpleUploadedFile(
            "big.log", content, content_type="text/plain",
        )

        client.post(reverse("submit:start"), data)

        attachment = Submission.objects.get().attachments.get()
        with attachment.file.open("rb") as fh:
            assert fh.read() == content

    def test_a_plain_users_submission_is_recorded_as_community(
        self, client, submitter, dell
    ):
        """The vendor tier is not offered to them, so it cannot be picked.

        This used to re-render the form with an error. It now accepts the
        submission at the tier they are entitled to instead: the level field is not
        shown to a plain member at all, so posting one means a tampered or stale
        form, and throwing the whole submission away over a control they were never
        given is the wrong trade. ``effective_level`` caps the same way at
        approval, so nothing is granted here either way.
        """
        data = self._payload(dell, claimed_validation_level=ValidationLevel.VENDOR)

        resp = client.post(reverse("submit:start"), data)

        assert resp.status_code == 302
        submission = Submission.objects.get()
        assert submission.claimed_validation_level == ValidationLevel.COMMUNITY

    def test_vendor_submitter_can_claim_vendor_level(self, client, submitter, dell):
        VendorMembership.objects.create(
            user=submitter, vendor=dell, role=VendorMembership.ROLE_SUBMITTER
        )
        data = self._payload(
            dell,
            claimed_validation_level=ValidationLevel.VENDOR,
            on_behalf_of=dell.slug,
        )
        resp = client.post(reverse("submit:start"), data)
        assert resp.status_code == 302
        sub = Submission.objects.get()
        assert sub.on_behalf_of == dell
        assert sub.claimed_validation_level == ValidationLevel.VENDOR

    def test_notification_email_sent_to_reviewers(self, client, submitter, dell):
        client.post(reverse("submit:start"), self._payload(dell))
        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == ["reviewers@example.com"]

    def test_vendor_spec_url_is_persisted_on_listing(self, client, submitter, dell):
        data = self._payload(dell, vendor_spec_url="https://dell.example/specs/r750")
        client.post(reverse("submit:start"), data)
        system = System.objects.get(name="PowerEdge R750")
        assert system.vendor_spec_url == "https://dell.example/specs/r750"

    def test_vendor_spec_url_optional(self, client, submitter, dell):
        # Empty string must be accepted so submitters aren't forced to hunt
        # down a spec link for every listing.
        data = self._payload(dell, vendor_spec_url="")
        resp = client.post(reverse("submit:start"), data)
        assert resp.status_code == 302
        assert System.objects.get(name="PowerEdge R750").vendor_spec_url == ""

    def test_blank_model_number_is_accepted(self, client, submitter, dell):
        """CPU families and similar `Component` listings have no single SKU
        - only a family name. The submit form must accept a blank
        ``model_number`` so those listings can be created without lying
        about the model."""
        data = self._payload(dell, kind="component", name="AMD EPYC 9004 Series", model_number="")
        resp = client.post(reverse("submit:start"), data)
        assert resp.status_code == 302
        from lumina.hardware.models import Component
        # Scoped to this submitter: a data migration seeds a CPU family with this
        # exact name, so a bare get() by name finds two and raises. The collision is
        # the point of the test - the name really is a family, not a SKU.
        comp = Component.objects.get(
            name="AMD EPYC 9004 Series", created_by=submitter,
        )
        assert comp.model_number == ""

    def test_release_compatibility_is_persisted(self, client, submitter, dell):
        from lumina.releases.models import AlmaLinuxRelease

        AlmaLinuxRelease.objects.create(major=9)
        AlmaLinuxRelease.objects.create(major=10)

        data = self._payload(
            dell,
            release_support_9="on",
            release_support_10="on",
        )
        resp = client.post(reverse("submit:start"), data)
        assert resp.status_code == 302
        system = System.objects.get(name="PowerEdge R750")
        bindings = {v.release.major for v in system.versions.all()}
        assert bindings == {9, 10}

    def test_unchecked_release_is_not_attached(self, client, submitter, dell):
        from lumina.hardware.models import ListingVersion
        from lumina.releases.models import AlmaLinuxRelease

        alma9 = AlmaLinuxRelease.objects.create(major=9)
        AlmaLinuxRelease.objects.create(major=10)

        data = self._payload(
            dell,
            # Only AlmaLinux 9 is supported; 10's minor is present but
            # without the support checkbox it must be ignored.
            release_support_9="on",
        )
        client.post(reverse("submit:start"), data)
        system = System.objects.get(name="PowerEdge R750")
        assert list(system.versions.all()) == [
            ListingVersion.objects.get(listing_system=system, release=alma9),
        ]
        assert system.versions.get().release.major == 9


class ProposedCategoryValuesTests:
    def test_propose_new_value_creates_pending(self, client, submitter, dell):
        Category.objects.create(name="Architecture", slug="architecture")
        data = {
            "kind": "system",
            "name": "RISC-V Dev Board",
            "model_number": "dev1",
            "vendor": dell.slug,
            "claimed_validation_level": ValidationLevel.COMMUNITY,
            "propose_architecture": "riscv64",
        }
        client.post(reverse("submit:start"), data)
        val = CategoryValue.objects.get(value="riscv64")
        assert val.status == CategoryValue.STATUS_PENDING
        assert val.proposed_by == submitter


class NoRevalidationThroughThisFormTests:
    """The form cannot address a listing it did not create. It used to.

    Posting ``listing_slug`` made the form reuse an existing listing, and because
    ``_attach_release_versions`` ran from ``save()`` rather than from ``approve()``, the
    write landed *before* any reviewer saw the submission. There was no ownership check
    and no ``published`` filter, so any logged-in account could name any listing and
    ``update_or_create`` its ``ListingVersion`` rows. Driven end to end, a brand-new
    user with no vendor membership rewrote a Dell-owned, run-proven "AlmaLinux 9"
    row down to "9.1" while their submission still sat pending. The row kept
    ``source='run'``, so the public page and the API attributed the downgrade to a
    validation run that never happened; rejecting the submission did not revert it, and
    a later genuine 9.6 run could not repair it, because ``record_compatibility`` only
    ever lowers a floor toward proven ground.

    Re-validating a listing means running the suite against it.

    These are regression guards, not tests of a feature. The class earns its keep by
    failing if the field ever comes back.
    """

    def test_the_form_has_no_listing_slug_field(self, submitter):
        from lumina.hardware.forms import SubmissionForm

        assert "listing_slug" not in SubmissionForm(user=submitter).fields

    def test_posting_a_listing_slug_does_not_touch_that_listing(
        self, client, submitter, dell
    ):
        """The payload from the original exploit, replayed.

        An unknown field is ignored by a Django form, so the post is simply treated as
        a new listing. What matters is that the named listing is left exactly as it
        was, and specifically that its release floor does not move.
        """
        from lumina.hardware.models import ListingVersion
        from lumina.releases.models import AlmaLinuxRelease

        release, _ = AlmaLinuxRelease.objects.get_or_create(
            major=9, defaults={"supported": True},
        )
        victim = System.objects.create(
            name="PowerEdge R750", vendor=dell, model_number="R750",
            published=True, validation_level=ValidationLevel.VENDOR,
        )
        proven = ListingVersion.objects.create(
            listing_system=victim, release=release,
            source=ListingVersion.SOURCE_RUN,
        )

        client.post(reverse("submit:start"), {
            "kind": "system",
            "listing_slug": victim.slug,
            "name": "Something Else",
            "vendor": dell.slug,
            "claimed_validation_level": ValidationLevel.COMMUNITY,
            "release_support_9": "1",
        })

        proven.refresh_from_db()
        # The floor this used to guard is gone with per-minor certification; what a submission
        # must still never do is claim a release as *proven*.
        assert proven.source == ListingVersion.SOURCE_RUN
        assert not Submission.objects.filter(listing_system=victim).exists()
        assert victim.versions.count() == 1

    def test_a_nonexistent_slug_is_not_a_server_error(self, client, submitter, dell):
        """``model.objects.get(slug=...)`` on a free-text field was an uncaught
        ``DoesNotExist``, so a typo in that box returned HTTP 500."""
        resp = client.post(reverse("submit:start"), {
            "kind": "system",
            "listing_slug": "no-such-listing-anywhere",
            "name": "Fresh Listing",
            "vendor": dell.slug,
            "claimed_validation_level": ValidationLevel.COMMUNITY,
        })

        assert resp.status_code < 500
