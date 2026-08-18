"""Trust-tier comparison, shared by the hardware and software catalogs.

- The tiers live in one place so neither subsystem imports the other.
- ``highest_level`` collapses a set of tiers to the one a listing badge shows.
- The three tiers are **totally ordered**: community < almalinux < vendor. Vendor
  overrides AlmaLinux, so there is no tie to break and no iteration-order
  dependence to guard against.
- An empty set is the community floor, not an error, because a software listing
  can transiently have no approved compatibility rows.
"""
from __future__ import annotations

from lumina.core.certification import ValidationLevel, highest_level, level_outranks


def test_a_single_tier_is_its_own_highest():
    assert highest_level([ValidationLevel.VENDOR]) == ValidationLevel.VENDOR


def test_vendor_and_almalinux_both_outrank_community():
    assert highest_level(
        [ValidationLevel.COMMUNITY, ValidationLevel.VENDOR]
    ) == ValidationLevel.VENDOR
    assert highest_level(
        [ValidationLevel.COMMUNITY, ValidationLevel.ALMALINUX]
    ) == ValidationLevel.ALMALINUX


def test_vendor_overrides_almalinux():
    """Vendor is the higher tier, whichever order the two arrive in.

    They used to share rank 1 and be separated by a documented tie preference that
    favoured AlmaLinux. They are now genuinely ordered, which means one rule
    governs the badge, ``level_outranks``, and every upgrade path instead of the
    display and the comparison disagreeing.
    """
    forwards = highest_level([ValidationLevel.VENDOR, ValidationLevel.ALMALINUX])
    backwards = highest_level([ValidationLevel.ALMALINUX, ValidationLevel.VENDOR])

    assert forwards == backwards == ValidationLevel.VENDOR


def test_no_tiers_at_all_is_the_community_floor():
    """A software listing whose only compatibility row is still pending has no
    approved tier to report; the floor is honest and a crash is not."""
    assert highest_level([]) == ValidationLevel.COMMUNITY


def test_duplicates_do_not_change_the_answer():
    assert highest_level(
        [ValidationLevel.COMMUNITY] * 5 + [ValidationLevel.VENDOR]
    ) == ValidationLevel.VENDOR


def test_the_tiers_are_totally_ordered():
    """community < almalinux < vendor, and nothing outranks itself.

    Strictness still matters: a >= test would let a second run at the *same* tier
    count as an upgrade, which is how a no-op re-validation would look like
    progress.
    """
    assert level_outranks(ValidationLevel.VENDOR, ValidationLevel.COMMUNITY)
    assert level_outranks(ValidationLevel.ALMALINUX, ValidationLevel.COMMUNITY)
    assert level_outranks(ValidationLevel.VENDOR, ValidationLevel.ALMALINUX)
    assert not level_outranks(ValidationLevel.ALMALINUX, ValidationLevel.VENDOR)
    for level in ValidationLevel.values:
        assert not level_outranks(level, level)
