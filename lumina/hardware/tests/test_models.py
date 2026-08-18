"""Tests for hardware models.

Listing shape:

- ``System`` and ``Component`` are separate concrete models that each store a
  name, vendor (FK to vendors.Vendor), model string, description, slug,
  created/updated timestamps, published flag, and validation_level.
- A new listing starts with ``published=False`` and validation_level=``community``.
- Slug is auto-generated from name + vendor; collisions get a numeric suffix.
- ``System.related_components`` is an optional M2M to ``Component`` for
  loose cross-reference only - neither depends on the other.

Category values on a listing:

- ``ListingCategoryValue`` binds a listing (System or Component, resolved via
  generic FK or separate FKs - implementation choice) to approved
  CategoryValues, including one or more AlmaLinux-version values (multi-
  version support).

Submission state machine:

- Submission starts in ``pending``.
- ``approve`` (by reviewer) → ``approved``, sets reviewed_by / reviewed_at,
  publishes the attached listing, and records one ``CommunityAttestation`` per release
  in ``cited_releases`` that the listing has a ``ListingVersion`` for. It does **not**
  write ``validation_level`` directly: that column is derived from the attestations by
  ``recompute_listing_levels``, so a submission citing no release publishes the listing
  and leaves it at the COMMUNITY floor.
- The tier is capped at ``Submission.MANUAL_CEILING`` (community), whatever the
  reviewer asks for and whatever the submitter claimed. A submission is a declaration;
  the vendor and AlmaLinux badges come from an approved suite run.
- ``reject`` → ``rejected``, sets reviewed_by, does NOT publish.
- ``request_changes`` → ``needs-changes``.
- Illegal transitions (approve an already-approved submission, etc.) raise.

Validation-level derivation (`derive_allowed_levels`):

- Anonymous / no user → raises (must be authenticated to submit).
- Plain authenticated user → ``[community]``.
- User with submit-role membership in a *verified* vendor → ``[community, vendor]``.
- User with submit-role membership in an *unverified* vendor → ``[community]``.
- User in the Django ``admin`` group → ``[community, vendor, almalinux]``.
- The derived list is what the submit form's "claimed level" dropdown shows. On this
  path it decides what may be *claimed*; what is finally *awarded* is additionally
  capped by ``MANUAL_CEILING`` above.

Community attestation:

- Attestations are keyed on (``ListingVersion``, person), so the same person
  confirming two releases counts twice and confirming one release twice counts once.
- A submission never lowers an existing attestation by the same person, which is what
  keeps a declaration from overwriting that person's run-proven result.

Two paragraphs describing re-validation *through this form* used to sit here, one of
them asserting that a vendor member's submission upgraded a listing to
Vendor-validated. Both described the behaviour that made this path unsafe; see
``lumina/hardware/forms.py``'s module docstring.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group

from lumina.core.certification import ValidationLevel
from lumina.hardware.models import (
    CommunityAttestation,
    Component,
    ListingVersion,
    Submission,
    System,
)
from lumina.hardware.services import recompute_listing_levels
from lumina.releases.models import AlmaLinuxRelease
from lumina.vendors.models import Vendor, VendorMembership
from lumina.vendors.services import derive_allowed_levels

pytestmark = pytest.mark.django_db
User = get_user_model()


def cite_release(system, major=9):
    """Give a listing an AlmaLinux release to be validated *on*.

    Validation is per release now, so approving a submission attaches its tier to
    the releases the listing cites. A listing citing none publishes and attests
    nothing - deliberately, and pinned by
    ``test_submission_approval.py::test_a_submission_citing_no_release_attests_nothing``.

    The tests below predate that and were written when a submission set the
    listing's tier directly. Their intents still hold; they just need a release for
    the tier to land on.
    """
    release, _ = AlmaLinuxRelease.objects.get_or_create(
        major=major, defaults={"supported": True},
    )
    return ListingVersion.objects.create(
        listing_system=system, release=release,
        source=ListingVersion.SOURCE_DECLARED,
    )


def submit(system, submitter, **kwargs):
    """A pending submission that cites what the listing carries.

    ``cited_releases`` has to be set explicitly, which is the point of it existing:
    ``approve`` used to read the releases back off the listing, so a submission was
    credited with every release the listing had ever carried rather than the ones it
    claimed. Constructing a Submission by hand no longer implies a claim.

    Defaulting to the listing's own versions is what ``SubmissionForm.save`` does in
    effect - it writes those rows and cites exactly them - so these tests keep
    exercising the ordinary case. Pass ``cites=[]`` for a submission that claims
    nothing.
    """
    cites = kwargs.pop("cites", None)
    kwargs.setdefault("claimed_validation_level", ValidationLevel.COMMUNITY)
    submission = Submission.objects.create(
        submitter=submitter, listing_system=system, **kwargs,
    )
    if cites is None:
        cites = [v.release for v in system.versions.select_related("release")]
    submission.cited_releases.set(cites)
    return submission


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def submitter():
    return User.objects.create_user(username="submitter")


@pytest.fixture
def reviewer():
    u = User.objects.create_user(username="reviewer")
    g, _ = Group.objects.get_or_create(name="reviewer")
    u.groups.add(g)
    return u


@pytest.fixture
def admin():
    u = User.objects.create_user(username="admin", is_staff=True, is_superuser=True)
    g, _ = Group.objects.get_or_create(name="admin")
    u.groups.add(g)
    return u


@pytest.fixture
def dell():
    return Vendor.objects.create(name="Dell", verified=True)


@pytest.fixture
def unverified_vendor():
    return Vendor.objects.create(name="Unverified Co", verified=False)


@pytest.fixture
def system(dell, submitter):
    return System.objects.create(
        name="PowerEdge R750",
        vendor=dell,
        model_number="R750",
        created_by=submitter,
    )


@pytest.fixture
def component(dell, submitter):
    return Component.objects.create(
        name="BCM57414 25GbE NIC",
        vendor=dell,
        model_number="BCM57414",
        created_by=submitter,
    )


# --------------------------------------------------------------------------- #
# Listings
# --------------------------------------------------------------------------- #
class ListingDefaultsTests:
    def test_slug_autoset_includes_vendor_and_name(self, system):
        assert "dell" in system.slug
        assert "poweredge-r750" in system.slug

    def test_slug_collision_suffix(self, dell, submitter):
        a = System.objects.create(name="Same Name", vendor=dell, model_number="A", created_by=submitter)
        b = System.objects.create(name="Same Name", vendor=dell, model_number="B", created_by=submitter)
        assert a.slug != b.slug

    def test_related_components_optional(self, system, component):
        assert list(system.related_components.all()) == []
        system.related_components.add(component)
        assert list(system.related_components.all()) == [component]


# --------------------------------------------------------------------------- #
# Submission state machine
# --------------------------------------------------------------------------- #
class SubmissionStateMachineTests:
    def test_approve_publishes_listing_and_sets_level(self, system, submitter, reviewer):
        sub = Submission.objects.create(
            submitter=submitter,
            listing_system=system,
            claimed_validation_level=ValidationLevel.COMMUNITY,
        )
        sub.approve(by=reviewer, final_level=ValidationLevel.COMMUNITY)
        system.refresh_from_db()
        assert sub.status == Submission.STATUS_APPROVED
        assert sub.reviewed_by == reviewer
        assert sub.reviewed_at is not None
        assert system.published is True
        # No level assertion here: this submission cites no release, so
        # ``recompute_listing_levels`` rolls up an empty list to the COMMUNITY floor,
        # which is also the field default - deleting the write in
        # ``hardware/services.py`` left it green. The tier is covered by
        # test_submission_approval.py, which cites a release first.

    def test_approve_caps_the_final_level_at_the_manual_ceiling(
        self, system, submitter, reviewer
    ):
        """A reviewer cannot certify a declaration above community, and this is where
        the rule has to live.

        It used to be ``test_approve_with_override_sets_final_level``, asserting that
        whatever tier was passed became the listing's tier. Nothing checked where the
        tier came from: ``review:approve`` read it straight from the POST body and
        validated only that the string was one of the three enum values, so a
        submission from an account with no vendor membership, no staff flag, and no
        uploaded evidence came out Vendor-validated. An empty POST body did it too, by
        falling back to the submitter's own claim.

        Capped on the model rather than in the view because the view is one door: a
        direct ``approve(final_level=VENDOR)`` was enough on its own.
        """
        cite_release(system)

        submit(system, submitter).approve(
            by=reviewer, final_level=ValidationLevel.VENDOR,
        )

        system.refresh_from_db()
        assert system.validation_level == Submission.MANUAL_CEILING
        assert system.validation_level == ValidationLevel.COMMUNITY

    def test_the_ceiling_is_the_lowest_tier(self):
        """Stated as a literal so raising ``MANUAL_CEILING`` cannot pass unnoticed.

        The test above compares the outcome against the constant, so on its own it
        would follow that constant wherever it moved and keep passing.
        """
        assert Submission.MANUAL_CEILING == "community"

    def test_reject_does_not_publish(self, system, submitter, reviewer):
        sub = Submission.objects.create(
            submitter=submitter,
            listing_system=system,
            claimed_validation_level=ValidationLevel.COMMUNITY,
        )
        sub.reject(by=reviewer, reason="insufficient evidence")
        system.refresh_from_db()
        assert sub.status == Submission.STATUS_REJECTED
        assert system.published is False

    def test_request_changes(self, system, submitter, reviewer):
        sub = Submission.objects.create(
            submitter=submitter,
            listing_system=system,
            claimed_validation_level=ValidationLevel.COMMUNITY,
        )
        sub.request_changes(by=reviewer, reason="attach kernel version")
        assert sub.status == Submission.STATUS_NEEDS_CHANGES

    def test_cannot_approve_twice(self, system, submitter, reviewer):
        sub = Submission.objects.create(
            submitter=submitter,
            listing_system=system,
            claimed_validation_level=ValidationLevel.COMMUNITY,
        )
        sub.approve(by=reviewer, final_level=ValidationLevel.COMMUNITY)
        with pytest.raises(ValueError):
            sub.approve(by=reviewer, final_level=ValidationLevel.COMMUNITY)

    def test_cannot_reject_already_rejected(self, system, submitter, reviewer):
        sub = Submission.objects.create(
            submitter=submitter,
            listing_system=system,
            claimed_validation_level=ValidationLevel.COMMUNITY,
        )
        sub.reject(by=reviewer, reason="x")
        with pytest.raises(ValueError):
            sub.reject(by=reviewer, reason="y")


# --------------------------------------------------------------------------- #
# Validation-level derivation (who can claim what)
# --------------------------------------------------------------------------- #
class ValidationLevelDerivationTests:
    def test_anonymous_raises(self):
        from django.contrib.auth.models import AnonymousUser
        with pytest.raises(PermissionError):
            derive_allowed_levels(AnonymousUser(), vendor=None)

    def test_plain_user_gets_user_only(self, submitter):
        assert derive_allowed_levels(submitter, vendor=None) == [ValidationLevel.COMMUNITY]

    def test_verified_vendor_submitter_gets_vendor(self, submitter, dell):
        VendorMembership.objects.create(
            user=submitter, vendor=dell, role=VendorMembership.ROLE_SUBMITTER
        )
        levels = derive_allowed_levels(submitter, vendor=dell)
        assert ValidationLevel.COMMUNITY in levels
        assert ValidationLevel.VENDOR in levels
        assert ValidationLevel.ALMALINUX not in levels

    def test_unverified_vendor_blocked_from_vendor_level(self, submitter, unverified_vendor):
        VendorMembership.objects.create(
            user=submitter, vendor=unverified_vendor,
            role=VendorMembership.ROLE_SUBMITTER,
        )
        levels = derive_allowed_levels(submitter, vendor=unverified_vendor)
        assert levels == [ValidationLevel.COMMUNITY]

    def test_member_role_blocked_from_vendor_level(self, submitter, dell):
        VendorMembership.objects.create(
            user=submitter, vendor=dell, role=VendorMembership.ROLE_MEMBER
        )
        levels = derive_allowed_levels(submitter, vendor=dell)
        assert levels == [ValidationLevel.COMMUNITY]

    def test_admin_group_can_claim_almalinux(self, admin):
        levels = derive_allowed_levels(admin, vendor=None)
        assert levels == [ValidationLevel.COMMUNITY, ValidationLevel.ALMALINUX]

    def test_an_admin_gets_no_vendor_tier_without_naming_a_vendor(self, admin):
        """The tier is tied to attribution, not to standing.

        An unconditional vendor grant let a run say "vendor-validated" without
        saying which vendor was doing the validating - the one thing that claim
        has to carry.
        """
        assert ValidationLevel.VENDOR not in derive_allowed_levels(admin, vendor=None)

    def test_an_admin_acting_for_a_vendor_does_get_the_vendor_tier(self, admin, dell):
        """Admins may act for anyone, so naming a vendor is enough for them."""
        levels = derive_allowed_levels(admin, vendor=dell)

        assert levels[-1] == ValidationLevel.VENDOR, "vendor must be the default"


# --------------------------------------------------------------------------- #
# Community attestations
# --------------------------------------------------------------------------- #
class CommunityAttestationTests:
    def _approved_listing(self, system, submitter, reviewer):
        cite_release(system)
        submit(system, submitter).approve(
            by=reviewer, final_level=ValidationLevel.COMMUNITY,
        )
        return system

    def test_revalidation_increments_attestation_count(self, system, submitter, reviewer):
        listing = self._approved_listing(system, submitter, reviewer)
        assert listing.attestation_count == 1

        other = User.objects.create_user(username="another")
        submit(listing, other).approve(
            by=reviewer, final_level=ValidationLevel.COMMUNITY,
        )
        listing.refresh_from_db()
        assert listing.attestation_count == 2

    def test_a_vendor_members_submission_is_still_only_a_declaration(
        self, system, submitter, reviewer, dell
    ):
        """Submitting on behalf of a verified vendor does not certify the hardware.

        This test used to assert the opposite: that a vendor member's submission
        upgraded the listing to Vendor-validated. It did, and that was the defect. The
        vendor badge is a claim about verified evidence, and this path verifies
        nothing - no manifest, no digest checked against one, no test output parsed,
        and until recently not even a stored hash of the file uploaded. A submitter
        with no membership at all reached the same tier just by having a reviewer post
        it, so the badge distinguished nobody.

        A vendor earns the badge the same way anyone does now: by running the suite.
        """
        listing = self._approved_listing(system, submitter, reviewer)

        vendor_user = User.objects.create_user(username="vendor_user")
        VendorMembership.objects.create(
            user=vendor_user, vendor=dell, role=VendorMembership.ROLE_SUBMITTER
        )
        submission = submit(
            listing, vendor_user, on_behalf_of=dell,
            claimed_validation_level=ValidationLevel.VENDOR,
        )
        submission.approve(by=reviewer, final_level=ValidationLevel.VENDOR)

        listing.refresh_from_db()
        assert listing.validation_level == ValidationLevel.COMMUNITY
        attestation = CommunityAttestation.objects.get(attested_by=vendor_user)
        assert attestation.level == ValidationLevel.COMMUNITY

    def test_a_submission_cannot_downgrade_a_run_proven_attestation(
        self, system, submitter, reviewer, dell
    ):
        """The reachable half of the upgrade-or-leave-alone rule in ``approve``.

        Its original form set up the vendor tier with a *submission*, which the
        community ceiling now makes impossible - so the rule looked like it had become
        dead code. It has not. Attestations are keyed on (version, person), and runs
        write them too: someone whose vendor-tier run already proved release 9 can go
        on to file a manual submission citing 9, and ``get_or_create`` finds their
        existing row. Overwriting it would let a declaration erase a validated result
        for the same person, which is the more dangerous direction of the two.
        """
        version = cite_release(system)
        vendor_user = User.objects.create_user(username="vu")
        VendorMembership.objects.create(
            user=vendor_user, vendor=dell, role=VendorMembership.ROLE_SUBMITTER
        )
        # A pre-existing vendor-tier attestation by this person on this release. In
        # production it is an approved run that puts one here; the row is built
        # directly because the guard keys on (version, attested_by) and does not care
        # which source wrote it, and a real run needs a whole bundle. Hung off a
        # Submission only to satisfy ``attestation_exactly_one_source``.
        CommunityAttestation.objects.create(
            version=version, listing_system=system, attested_by=vendor_user,
            level=ValidationLevel.VENDOR,
            submission=Submission.objects.create(
                submitter=vendor_user, on_behalf_of=dell, listing_system=system,
                claimed_validation_level=ValidationLevel.VENDOR,
            ),
        )
        recompute_listing_levels(system)
        system.refresh_from_db()
        assert system.validation_level == ValidationLevel.VENDOR

        submit(system, vendor_user, on_behalf_of=dell).approve(
            by=reviewer, final_level=ValidationLevel.COMMUNITY,
        )

        system.refresh_from_db()
        assert system.validation_level == ValidationLevel.VENDOR
        assert system.attestation_count == 1


def test_the_community_tier_is_named_community_not_user():
    """The badge said "User" beside a heading reading "Community-validated".

    The tier means evidence from the AlmaLinux community, and a stream of
    independent attestations is what makes it trustworthy - which "user"
    obscures by suggesting one person. Value as well as label, because the value
    is what the API publishes and what every future reader of this code sees.
    """
    assert ValidationLevel.COMMUNITY.label == "Community-validated"
    assert ValidationLevel.COMMUNITY.value == "community"
    assert "user" not in {level.value for level in ValidationLevel}


def test_no_page_still_says_user_validated(client, db):
    """Two templates hardcoded the wording instead of reading the label."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[3] / "templates"
    offenders = [
        str(path.relative_to(root))
        for path in root.rglob("*.html")
        if "User-validated" in path.read_text(encoding="utf-8")
        or ">User<" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
