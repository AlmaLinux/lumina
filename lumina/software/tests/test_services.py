"""Attesting, withdrawing, and citing a major the vendor never did.

Confirming that software works is meant to be one click with nothing else asked,
so these services carry the guards a form would otherwise have to:

- one attestation per user per major, and a second click is a no-op not an error
- attesting requires an approved major; a pending community report is not
  something strangers can pile onto before a reviewer sees it
- reporting a *new* major is bounded by the releases the Foundation has created,
  so nobody can invent AlmaLinux 12
- a rejected report is deleted rather than parked, so one bad early report cannot
  permanently block a genuine one later
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from lumina.core.certification import ValidationLevel
from lumina.releases.models import AlmaLinuxRelease
from lumina.software import services
from lumina.software.models import (
    Software,
    SoftwareAttestation,
    SoftwareCertification,
    SoftwareCompatibility,
)
from lumina.vendors.models import Vendor

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture(autouse=True)
def releases():
    for major in (8, 9, 10):
        AlmaLinuxRelease.objects.get_or_create(major=major,
                                               defaults={"supported": True})


@pytest.fixture
def backup():
    vendor = Vendor.objects.create(name="Acme Software", scope=Vendor.SCOPE_SOFTWARE)
    product = Software.objects.create(
        vendor=vendor, name="Acme Backup", published=True,
    )
    for major in (8, 9):
        SoftwareCompatibility.objects.create(
            software=product, release=AlmaLinuxRelease.objects.get(major=major),
        )
    return product


@pytest.fixture
def fan():
    return User.objects.create_user("fan", email="fan@example.com")


def _release(major):
    return AlmaLinuxRelease.objects.get(major=major)


# --- attesting ----------------------------------------------------------------


def test_attesting_records_one_confirmation(backup, fan):
    services.attest(software=backup, release=_release(9), user=fan)

    row = backup.compatibility.get(release__major=9)
    assert row.attestations.count() == 1


def test_clicking_twice_is_a_no_op_not_an_error(backup, fan):
    """A double click on a one-click control must not 500 or double-count."""
    services.attest(software=backup, release=_release(9), user=fan)
    services.attest(software=backup, release=_release(9), user=fan)

    assert backup.compatibility.get(release__major=9).attestations.count() == 1


def test_the_same_user_may_confirm_a_different_major(backup, fan):
    """The cap is per major, which is the whole point of per-major validation."""
    services.attest(software=backup, release=_release(9), user=fan)
    services.attest(software=backup, release=_release(8), user=fan)

    assert SoftwareAttestation.objects.filter(user=fan).count() == 2


def test_withdrawing_removes_only_that_users_confirmation(backup, fan):
    other = User.objects.create_user("other", email="o@example.com")
    services.attest(software=backup, release=_release(9), user=fan)
    services.attest(software=backup, release=_release(9), user=other)

    services.withdraw_attestation(software=backup, release=_release(9), user=fan)

    row = backup.compatibility.get(release__major=9)
    assert row.attestations.count() == 1
    assert row.attestations.get().user == other


def test_withdrawing_when_you_never_confirmed_is_harmless(backup, fan):
    services.withdraw_attestation(software=backup, release=_release(9), user=fan)

    assert backup.compatibility.get(release__major=9).attestations.count() == 0


def test_attesting_a_major_the_listing_does_not_cite_is_refused(backup, fan):
    """Adding a major is a different, reviewed action - not a silent side effect
    of clicking confirm."""
    with pytest.raises(ValueError, match="does not cite"):
        services.attest(software=backup, release=_release(10), user=fan)


def test_attesting_a_pending_major_is_refused(backup, fan):
    """Strangers should not be able to pile onto a report a reviewer has not
    accepted yet."""
    reporter = User.objects.create_user("rep", email="rep@example.com")
    services.report_new_major(software=backup, release=_release(10), user=reporter)

    with pytest.raises(ValueError, match="awaiting review"):
        services.attest(software=backup, release=_release(10), user=fan)


def test_a_certified_major_can_still_be_confirmed(backup, fan):
    """Community confirmations sit alongside a vendor certification; they are not
    replaced by it."""
    nine = backup.compatibility.get(release__major=9)
    SoftwareCertification.objects.create(compatibility=nine,
                                        level=ValidationLevel.VENDOR)

    services.attest(software=backup, release=_release(9), user=fan)

    nine.refresh_from_db()
    assert nine.validation_level == ValidationLevel.VENDOR
    assert nine.attestations.count() == 1


# --- citing a new major -------------------------------------------------------


def test_reporting_a_new_major_creates_it_pending_with_the_reporters_confirmation(
    backup, fan
):
    """Both at once, so the reporter is not asked to click twice for one act."""
    row = services.report_new_major(software=backup, release=_release(10), user=fan)

    assert row.status == SoftwareCompatibility.STATUS_PENDING
    assert row.proposed_by == fan
    assert row.attestations.count() == 1


def test_reporting_a_major_the_listing_already_cites_is_refused(backup, fan):
    with pytest.raises(ValueError, match="already"):
        services.report_new_major(software=backup, release=_release(9), user=fan)


def test_reporting_an_unsupported_release_is_refused(backup, fan):
    """Bounded by what the Foundation has created, so nobody invents a release."""
    eol = AlmaLinuxRelease.objects.create(major=7, supported=False)

    with pytest.raises(ValueError, match="supported"):
        services.report_new_major(software=backup, release=eol, user=fan)


def test_approving_a_reported_major_publishes_it_with_its_confirmation(backup, fan):
    reviewer = User.objects.create_user("rev", email="rev@example.com")
    row = services.report_new_major(software=backup, release=_release(10), user=fan)

    row.approve(by=reviewer)

    row.refresh_from_db()
    assert row.status == SoftwareCompatibility.STATUS_APPROVED
    assert row.validation_level == ValidationLevel.COMMUNITY
    assert row.attestations.count() == 1
    assert backup.compatibility.approved().count() == 3


def test_rejecting_a_reported_major_deletes_it(backup, fan):
    """Parking it as rejected would block a genuine later report on the same
    major, once the software really does work there."""
    reviewer = User.objects.create_user("rev", email="rev@example.com")
    services.report_new_major(software=backup, release=_release(10), user=fan)

    services.reject_reported_major(
        software=backup, release=_release(10), by=reviewer, reason="Unverified.",
    )

    assert not backup.compatibility.filter(release__major=10).exists()
    # And the same major can be reported again.
    again = services.report_new_major(software=backup, release=_release(10), user=fan)
    assert again.status == SoftwareCompatibility.STATUS_PENDING


def test_rejecting_a_major_the_vendor_cited_is_refused(backup):
    """Only community reports are deletable this way; an approved major is the
    vendor's claim and needs the ordinary edit path."""
    reviewer = User.objects.create_user("rev", email="rev@example.com")

    with pytest.raises(ValueError, match="pending"):
        services.reject_reported_major(
            software=backup, release=_release(9), by=reviewer,
        )


# --- audit --------------------------------------------------------------------


def test_attesting_and_withdrawing_are_audited(backup, fan):
    from lumina.audit.models import AuditLogEntry

    services.attest(software=backup, release=_release(9), user=fan)
    services.withdraw_attestation(software=backup, release=_release(9), user=fan)

    actions = set(AuditLogEntry.objects.values_list("action", flat=True))
    assert {"software.attest", "software.attest_withdraw"} <= actions
