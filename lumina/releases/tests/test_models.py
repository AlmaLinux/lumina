"""Tests for the AlmaLinuxRelease model.

This model is an admin-curated reference of the AlmaLinux majors that exist - 8, 9, 10, … - so
hardware listings can bind to them with a minimum-minor version requirement. Kept separate from
the generic taxonomy because it's not user-proposable and has richer semantics (version
comparison).

``releases/0002_seed_releases`` puts the current majors in every database, this one included, so
these work against a table that already has rows in it: a major the seed does not own for the
cases that need a clean one, and presence rather than exact contents for the rest. A test that
creates 9 and asserts the table holds only 9 would be testing the absence of the seed.
"""
from __future__ import annotations

import pytest
from django.db import IntegrityError

from lumina.releases.models import AlmaLinuxRelease

pytestmark = pytest.mark.django_db

# Long out of support and not in the seed, so a test can own it outright.
RETIRED = 7


class AlmaLinuxReleaseTests:
    def test_major_is_unique(self):
        AlmaLinuxRelease.objects.create(major=RETIRED)
        with pytest.raises(IntegrityError):
            AlmaLinuxRelease.objects.create(major=RETIRED)

    def test_the_seed_put_the_current_majors_there(self):
        """The reason the rest of these are written the way they are, and worth asserting on its
        own: a production install with no releases has no release filter, nothing to tick on a
        submission, and nowhere for a run's evidence to hang."""
        assert {8, 9, 10} <= set(AlmaLinuxRelease.objects.values_list("major", flat=True))

    def test_default_ordering_is_newest_major_first(self):
        AlmaLinuxRelease.objects.create(major=RETIRED)

        majors = [release.major for release in AlmaLinuxRelease.objects.all()]

        assert majors == sorted(majors, reverse=True)
        assert majors[-1] == RETIRED, "the oldest major sorts last"

    def test_str_is_branded_name(self):
        release = AlmaLinuxRelease.objects.get(major=10)

        assert str(release) == "AlmaLinux 10"

    def test_supported_queryset_excludes_eol(self):
        eol = AlmaLinuxRelease.objects.create(major=RETIRED, supported=False)
        current = AlmaLinuxRelease.objects.get(major=10)

        supported = list(AlmaLinuxRelease.objects.supported())

        assert eol not in supported
        assert current in supported
