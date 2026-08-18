"""Tests for the AlmaLinuxRelease model.

This model is an admin-curated reference of the AlmaLinux majors that
exist - 8, 9, 10, … - so hardware listings can bind to them with a
minimum-minor version requirement. Kept separate from the generic
taxonomy because it's not user-proposable and has richer semantics
(version comparison).
"""
from __future__ import annotations

import pytest
from django.db import IntegrityError

from lumina.releases.models import AlmaLinuxRelease

pytestmark = pytest.mark.django_db


class AlmaLinuxReleaseTests:
    def test_major_is_unique(self):
        AlmaLinuxRelease.objects.create(major=10)
        with pytest.raises(IntegrityError):
            AlmaLinuxRelease.objects.create(major=10)

    def test_default_ordering_is_newest_major_first(self):
        AlmaLinuxRelease.objects.create(major=8)
        AlmaLinuxRelease.objects.create(major=10)
        AlmaLinuxRelease.objects.create(major=9)
        assert [r.major for r in AlmaLinuxRelease.objects.all()] == [10, 9, 8]

    def test_str_is_branded_name(self):
        r = AlmaLinuxRelease.objects.create(major=10)
        assert str(r) == "AlmaLinux 10"

    def test_supported_queryset_excludes_eol(self):
        AlmaLinuxRelease.objects.create(major=7, supported=False)
        v10 = AlmaLinuxRelease.objects.create(major=10, supported=True)
        assert list(AlmaLinuxRelease.objects.supported()) == [v10]
