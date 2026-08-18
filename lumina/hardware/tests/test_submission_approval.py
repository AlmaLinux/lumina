"""Approving a human hardware submission, now that a tier is per release.

Written when approval was unverified: ``test_models.py``'s classes were named
``TestFoo``, which ``pyproject.toml``'s ``python_classes`` does not match, so 18 of
its 20 tests never ran and a schema change quietly broke approval. Those classes
have since been renamed to the declared ``*Tests`` convention and now run, but
these stay - they cover the per-release behaviour directly and flat functions
cannot fall out of collection.

What is pinned down here:
- one attestation per release the submission cites, at the reviewer's final tier
- *only* the cited releases, not every release the listing happens to carry
- the listing's tier is the rollup of those, not a value written directly
- a submission citing no release publishes the listing and attests nothing, rather
  than inventing a release to attach evidence to
- a re-validation by the same person on the same release does not double-count

Every tier here is community, and that is the rule rather than the fixtures being
lazy: a manual submission is a declaration, capped at ``Submission.MANUAL_CEILING``.
These tests used to pass ``VENDOR`` to tell the reviewer's decision apart from the
default, which quietly documented a path to the top tier that needed no evidence at
all. See ``test_models.py::test_approve_caps_the_final_level_at_the_manual_ceiling``.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from lumina.core.certification import ValidationLevel
from lumina.hardware.models import (
    CommunityAttestation,
    ListingVersion,
    Submission,
    System,
)
from lumina.releases.models import AlmaLinuxRelease
from lumina.vendors.models import Vendor

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture(autouse=True)
def releases():
    for major in (8, 9, 10):
        AlmaLinuxRelease.objects.get_or_create(
            major=major, defaults={"supported": True},
        )


@pytest.fixture
def submitter():
    return User.objects.create_user("sub")


@pytest.fixture
def reviewer():
    return User.objects.create_user("rev")


@pytest.fixture
def system(submitter):
    vendor = Vendor.objects.create(name="Dell Inc.", published=True)
    return System.objects.create(
        name="PowerEdge R750", vendor=vendor, model_number="R750",
        created_by=submitter,
    )


def _cite(system, major):
    """A declared release, as ``SubmissionForm._attach_release_versions`` writes."""
    return ListingVersion.objects.create(
        listing_system=system,
        release=AlmaLinuxRelease.objects.get(major=major),
        source=ListingVersion.SOURCE_DECLARED,
    )


def _submit(system, submitter, level=ValidationLevel.COMMUNITY, cites=None):
    """A submission citing the listing's releases, as ``SubmissionForm.save`` does.

    ``cited_releases`` is explicit because ``approve`` no longer re-derives it from
    the listing. Pass ``cites`` to claim something other than everything the listing
    carries - the divergence between the two is what
    ``test_only_the_cited_releases_are_attested`` covers.
    """
    submission = Submission.objects.create(
        submitter=submitter, listing_system=system, claimed_validation_level=level,
    )
    if cites is None:
        cites = [v.release for v in system.versions.select_related("release")]
    submission.cited_releases.set(cites)
    return submission


def test_every_cited_release_gets_its_own_attestation(system, submitter, reviewer):
    _cite(system, 9)
    _cite(system, 10)

    _submit(system, submitter).approve(
        by=reviewer, final_level=ValidationLevel.COMMUNITY,
    )

    versions = {v.release.major: v for v in system.versions.select_related("release")}
    assert versions[9].attestations.count() == 1
    assert versions[10].attestations.count() == 1
    assert versions[9].validation_level == ValidationLevel.COMMUNITY
    assert versions[10].validation_level == ValidationLevel.COMMUNITY
    # Attributed to the submitter, not the reviewer: it is their claim.
    assert {a.attested_by for a in CommunityAttestation.objects.all()} == {submitter}
    # The minor floor the submitter declared is untouched by approval.
    assert versions[9].release.major == 9


def test_the_listing_tier_is_the_rollup_of_its_releases(system, submitter, reviewer):
    _cite(system, 9)

    _submit(system, submitter).approve(
        by=reviewer, final_level=ValidationLevel.COMMUNITY,
    )

    system.refresh_from_db()
    assert system.published is True
    assert system.validation_level == ValidationLevel.COMMUNITY
    assert system.attestation_count == 1


def test_a_submission_citing_no_release_attests_nothing(system, submitter, reviewer):
    """It still publishes. There is simply no release to attach evidence to, and
    inventing one would be a claim nobody made."""
    _submit(system, submitter).approve(
        by=reviewer, final_level=ValidationLevel.COMMUNITY,
    )

    system.refresh_from_db()
    assert system.published is True
    assert CommunityAttestation.objects.count() == 0
    # Floored rather than left null: the listing-level column is non-null and
    # drives a CSS class.
    assert system.validation_level == ValidationLevel.COMMUNITY


def test_a_revalidation_by_the_same_person_does_not_double_count(
    system, submitter, reviewer
):
    _cite(system, 9)
    _submit(system, submitter).approve(
        by=reviewer, final_level=ValidationLevel.COMMUNITY,
    )
    _submit(system, submitter).approve(
        by=reviewer, final_level=ValidationLevel.COMMUNITY,
    )

    system.refresh_from_db()
    assert system.attestation_count == 1
    assert system.validation_level == ValidationLevel.COMMUNITY


def test_only_the_cited_releases_are_attested(system, submitter, reviewer):
    """A submission attests what it claimed, not everything the listing carries.

    ``approve`` used to walk ``listing.versions.all()``, on the reasoning that the
    submit form had just written those rows so the listing was where they lived. That
    was true only for a listing the form created. Once a submission could name an
    existing listing, approving one attested every release that listing had ever
    carried: a submission citing *nothing* against a listing already recording 8 and
    10 minted two attestations, and the detail page counted them under "community
    members who independently confirmed it by running the suite".

    The re-validation flow is gone, so the walk would be correct again today. This
    pins the rule anyway, because "correct by coincidence" stops being correct the
    moment anything else can add a version to a draft listing - a reviewer tweak, an
    edit proposal, a future bulk import.
    """
    _cite(system, 8)
    _cite(system, 9)
    _cite(system, 10)

    # The submitter claims 9 only, though the listing carries three releases.
    _submit(
        system, submitter,
        cites=[AlmaLinuxRelease.objects.get(major=9)],
    ).approve(by=reviewer, final_level=ValidationLevel.COMMUNITY)

    attested = {
        a.version.release.major for a in CommunityAttestation.objects.all()
    }
    assert attested == {9}
    system.refresh_from_db()
    assert system.attestation_count == 1


def test_a_submission_citing_a_release_the_listing_lacks_attests_nothing(
    system, submitter, reviewer
):
    """Belt and braces on the join in ``approve``.

    Attestations hang off a ``ListingVersion``, so a cited release with no version row
    has nothing to attach to. Skipped rather than conjuring the row, because creating
    a version here would let a submission assert compatibility that the form never
    recorded and a reviewer never saw on the page.
    """
    _cite(system, 9)

    _submit(
        system, submitter,
        cites=[AlmaLinuxRelease.objects.get(major=10)],
    ).approve(by=reviewer, final_level=ValidationLevel.COMMUNITY)

    assert CommunityAttestation.objects.count() == 0
    assert system.versions.count() == 1


def test_two_people_submitting_the_same_release_both_count(
    system, submitter, reviewer
):
    _cite(system, 9)
    other = User.objects.create_user("other")
    _submit(system, submitter).approve(
        by=reviewer, final_level=ValidationLevel.COMMUNITY,
    )
    _submit(system, other).approve(
        by=reviewer, final_level=ValidationLevel.COMMUNITY,
    )

    system.refresh_from_db()
    assert system.attestation_count == 2
