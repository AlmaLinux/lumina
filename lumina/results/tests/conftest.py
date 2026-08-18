"""Shared fixtures for the certification-run tests.

Every run these tests ingest reports an AlmaLinux version, and ``ingest`` resolves
that to an ``AlmaLinuxRelease`` row by major - it looks one up, it never creates
one. With no releases in the database a run lands with ``alma_release=None``, so
``record_compatibility`` bails and no ``ListingVersion`` is written.

That was invisible while attestation was a per-listing fact. Now that an
attestation is *about a major*, a run with no resolvable release has nothing to
attest, so these tests need the releases the deployment actually has.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def alma_releases(db):
    """Releases 8, 9, and 10, matching the deployment's supported set."""
    from lumina.releases.models import AlmaLinuxRelease

    for major in (8, 9, 10):
        AlmaLinuxRelease.objects.get_or_create(
            major=major, defaults={"supported": True},
        )
