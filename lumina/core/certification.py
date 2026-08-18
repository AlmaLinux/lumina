"""Trust tiers, shared by the hardware and software catalogs.

These live here rather than in either catalog app because both need them and
neither should import the other. The software subsystem is meant to be
freestanding, and `from lumina.hardware.models import ValidationLevel` inside it
would be a dependency in the wrong direction.

Moving the enum out of ``hardware.models`` costs no migration: Django serializes
``choices`` as a literal list of tuples into each migration file, so the
historical migrations already carry their own copies and never import this
module.
"""
from __future__ import annotations

from collections.abc import Iterable

from django.db import models


class ValidationLevel(models.TextChoices):
    # "community", not "user": the tier means evidence from the AlmaLinux
    # community, and a stream of independent attestations is the thing that makes
    # it trustworthy - which "user" actively obscures by suggesting one person.
    # The stored value is renamed too, not just the label. It is the vocabulary
    # the API publishes and the word every future reader of this code sees, and
    # the system is pre-production, so the moment to get it right is now rather
    # than carrying a misleading key for the life of the project.
    COMMUNITY = "community", "Community-validated"
    VENDOR = "vendor", "Vendor-validated"
    ALMALINUX = "almalinux", "AlmaLinux-validated"


# Rank used to compare trust tiers; higher wins. The three are totally ordered.
#
# Vendor overrides AlmaLinux. The party that builds the hardware or ships the
# software is the one that has to keep supporting it, so its own certification is
# the stronger statement about whether the thing works and will keep working;
# AlmaLinux validating something the vendor never did is valuable evidence, but it
# is a third party vouching. Both remain visible on a detail page - this ordering
# only decides what a single badge says.
#
# These used to share rank 1, separated for display by a tie preference that
# favoured AlmaLinux. A total order means one rule governs the badge,
# ``level_outranks``, and every upgrade path, instead of the display and the
# comparison being able to disagree.
LEVEL_RANK = {
    ValidationLevel.COMMUNITY: 0,
    ValidationLevel.ALMALINUX: 1,
    ValidationLevel.VENDOR: 2,
}


# The short form of each tier: "Community", "AlmaLinux", "Vendor".
#
# For badges that sit in a column already headed "Validated by" or "Certified by".
# There, "-validated" on every badge of every row restates the column header once
# per cell, and the suffix is the widest part of the string, so it crowds the
# column while adding nothing a reader did not just read.
#
# The long labels stay on ``choices`` and remain the default everywhere else. A
# badge standing on its own - the headline on a detail page, a browse card, the
# review dropdowns, the API's ``*_display`` fields - has no column header to lean
# on and has to carry the whole meaning itself.
#
# One mapping rather than a conditional in each template: the certification
# results table had already grown its own inline
# ``{% if == "almalinux" %}AlmaLinux{% elif ... %}`` copy of exactly this.
SHORT_LABELS = {
    ValidationLevel.COMMUNITY: "Community",
    ValidationLevel.VENDOR: "Vendor",
    ValidationLevel.ALMALINUX: "AlmaLinux",
}


def short_label(level: str) -> str:
    """The badge text for a tier shown under a column that names the concept.

    Blank passes through rather than raising. Hardware's
    ``ListingVersion.validation_level`` uses ``""`` for "no tier recorded" (blank
    over NULL, per DJ001), and taking a page down over a display string would be a
    poor trade. An unrecognized tier *does* raise, because that means the enum grew
    a member this mapping never learned about - see
    ``test_every_tier_has_a_short_label``, which fails at that moment instead.
    """
    if not level:
        return ""
    return SHORT_LABELS[ValidationLevel(level)]


def level_outranks(a: str, b: str) -> bool:
    """Whether ``a`` is a strictly higher tier than ``b``.

    Strict on purpose: a >= test would make a second run at the same tier read as
    an upgrade, so a no-op re-validation would look like progress.
    """
    return LEVEL_RANK[ValidationLevel(a)] > LEVEL_RANK[ValidationLevel(b)]


def highest_level(levels: Iterable[str]) -> str:
    """The tier a single badge shows for a listing that holds several.

    Both catalogs need this: every AlmaLinux release a listing cites carries its
    own tier, while the browse card, the level CSS class, and the level filter all
    want one value.

    No tiebreak, because the tiers are totally ordered - ``max`` on rank is
    already deterministic.

    An empty input is the community floor rather than an error: a software listing
    whose only compatibility row is still awaiting review has no approved tier to
    report, and the floor is the honest answer.
    """
    ranked = {ValidationLevel(level) for level in levels}
    if not ranked:
        return ValidationLevel.COMMUNITY
    return max(ranked, key=lambda level: LEVEL_RANK[level])
