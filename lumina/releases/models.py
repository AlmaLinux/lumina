"""Admin-curated list of AlmaLinux major releases.

Hardware listings bind to one or more ``AlmaLinuxRelease`` rows via
``lumina.hardware.models.ListingVersion``. Certification is per **major**, so a row is
"certified for AlmaLinux 9" and nothing finer. Not a generic taxonomy category because:

1. Only the AlmaLinux OS Foundation defines them - submitters must not be
   able to propose "AlmaLinux 11" before it exists.
2. The minor still matters for *timing*, which needs a number rather than opaque text:
   hardware enablement proved on AlmaLinux Kitten lands in a specific upcoming minor, and
   ``latest_minor`` is what tells the catalog whether that minor has shipped yet.

A ``kitten_target_major`` flag briefly lived here, described as "how a Kitten run is matched
to the release it anticipates". It was not: a run's major comes from its own ``VERSION_ID``,
and nothing ever read the flag. An admin-editable checkbox that changes nothing is worse than
no checkbox, so it is gone. Nothing needs it - which major Kitten tracks is a fact the reports
carry themselves.
"""
from __future__ import annotations

from django.db import models


class AlmaLinuxReleaseQuerySet(models.QuerySet["AlmaLinuxRelease"]):
    def supported(self) -> AlmaLinuxReleaseQuerySet:
        return self.filter(supported=True)


class AlmaLinuxRelease(models.Model):
    major = models.PositiveSmallIntegerField(unique=True)
    supported = models.BooleanField(
        default=True,
        help_text="False for EOL releases. Excluded from supported() queryset.",
    )
    latest_minor = models.PositiveSmallIntegerField(
        null=True, blank=True, default=None,
        help_text=(
            "The newest minor of this major that has shipped. Raise it here when a new minor "
            "is released. Leave it EMPTY while the major itself has not been released at all - "
            "which is not the same as 0, and is the normal state for a major that AlmaLinux "
            "Kitten is tracking months before its first stable release. This is what lifts the "
            "disclaimer on hardware proved on Kitten."
        ),
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = AlmaLinuxReleaseQuerySet.as_manager()

    class Meta:
        ordering = ["-major"]
        # Django would derive "Alma linux release" from the class name, which
        # violates the brand rule that it is always written "AlmaLinux".
        verbose_name = "AlmaLinux release"
        verbose_name_plural = "AlmaLinux releases"

    def __str__(self) -> str:
        return f"AlmaLinux {self.major}"

    @property
    def is_released(self) -> bool:
        """Whether any minor of this major has shipped.

        ``latest_minor`` being empty is the distinction: 0 means "x.0 is out", empty means
        nothing is. AlmaLinux Kitten tracks a major for months before its first stable release -
        for 11 the gap is expected to run six months to a year - so hardware can be validated,
        certified, and published against a major nobody can install yet.
        """
        return self.latest_minor is not None

    def minor_is_live(self, minor: int | None) -> bool:
        """Whether ``minor`` of this major has shipped.

        ``None`` for ``minor`` means "no minor in particular", which is live as long as the major
        itself is out: a run on a stable release proved the hardware works on what people can
        already install.

        Nothing is live on an unreleased major, whatever minor is asked about.
        """
        if not self.is_released:
            return False
        if minor is None:
            return True
        return int(minor) <= self.latest_minor
