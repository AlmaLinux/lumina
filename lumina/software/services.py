"""Software catalog operations that span more than one model.

The per-major tiers are re-derived by ``Software.recompute_levels``, called from
``SoftwareCertification``'s own save and delete so no caller can leave them
stale. What lives here is the community-facing side: confirming that a product
works, withdrawing that, and citing a major the vendor never did.

These carry the guards a form would otherwise have to repeat, because confirming
is a single click with no form at all.
"""
from __future__ import annotations

from django.db import transaction

from lumina.audit.services import log_action
from lumina.releases.models import AlmaLinuxRelease
from lumina.software.models import (
    Software,
    SoftwareAttestation,
    SoftwareCertification,
    SoftwareCompatibility,
)


def _cited_major(software: Software, release: AlmaLinuxRelease) -> SoftwareCompatibility:
    """The listing's row for this major, or a refusal explaining which it is.

    Two different "no" answers, because they need different UI: a major the
    listing has never cited is an invitation to report it, while one awaiting
    review is an invitation to wait.
    """
    row = software.compatibility.filter(release=release).first()
    if row is None:
        raise ValueError(
            f"{software.name} does not cite AlmaLinux {release.major}. "
            "Report it as working there instead."
        )
    if row.status != SoftwareCompatibility.STATUS_APPROVED:
        raise ValueError(
            f"AlmaLinux {release.major} on {software.name} is still awaiting "
            "review."
        )
    return row


@transaction.atomic
def attest(
    *, software: Software, release: AlmaLinuxRelease, user, note: str = ""
) -> SoftwareAttestation:
    """Record that ``user`` confirms ``software`` works on this major.

    ``get_or_create`` rather than ``create``: this is behind a one-click control,
    a double click is an accident rather than an attempt at fraud, and the
    per-user uniqueness is already enforced by the database.
    """
    row = _cited_major(software, release)
    attestation, created = SoftwareAttestation.objects.get_or_create(
        compatibility=row, user=user, defaults={"note": note},
    )
    if created:
        log_action("software.attest", target=software, actor=user,
                   after={"major": release.major})
    return attestation


@transaction.atomic
def withdraw_attestation(
    *, software: Software, release: AlmaLinuxRelease, user
) -> None:
    """Remove ``user``'s own confirmation. Silent if they never gave one."""
    row = _cited_major(software, release)
    deleted, _ = SoftwareAttestation.objects.filter(
        compatibility=row, user=user
    ).delete()
    if deleted:
        log_action("software.attest_withdraw", target=software, actor=user,
                   after={"major": release.major})


@transaction.atomic
def report_new_major(
    *, software: Software, release: AlmaLinuxRelease, user
) -> SoftwareCompatibility:
    """Cite a major the listing does not have, pending review.

    This is the abandonment case at its sharpest: AlmaLinux 11 ships, the vendor
    has not touched their listing, and a user knows the product works. Without
    this, that fact stays invisible until the vendor acts - which for an abandoned
    listing is never.

    Reviewed once, because it adds a major to somebody else's listing. Every
    confirmation after approval is one click.

    The reporter's own confirmation is created alongside the row, so approval
    reveals both together and one act needs one click.
    """
    if not AlmaLinuxRelease.objects.supported().filter(pk=release.pk).exists():
        raise ValueError(
            f"AlmaLinux {release.major} is not a supported release."
        )
    if software.compatibility.filter(release=release).exists():
        raise ValueError(
            f"{software.name} already cites AlmaLinux {release.major}."
        )

    row = SoftwareCompatibility.propose(
        software=software, release=release, proposed_by=user,
    )
    SoftwareAttestation.objects.create(compatibility=row, user=user)
    log_action(
        "software.compatibility_propose", target=software, actor=user,
        after={"major": release.major},
    )
    return row


@transaction.atomic
def reject_reported_major(
    *, software: Software, release: AlmaLinuxRelease, by, reason: str = ""
) -> None:
    """Turn down a community-reported major by deleting the row.

    Deleted rather than parked as rejected: the same major may genuinely work
    later, and a rejected row occupying the unique (software, release) slot would
    block anyone from ever saying so.
    """
    row = software.compatibility.filter(release=release).first()
    if row is None or row.status != SoftwareCompatibility.STATUS_PENDING:
        raise ValueError(
            f"AlmaLinux {release.major} on {software.name} is not a pending "
            "community report."
        )
    log_action(
        "software.compatibility_reject", target=software, actor=by,
        before={"major": release.major, "proposed_by": str(row.proposed_by)},
        notes=reason,
    )
    row.delete()
    software.recompute_levels()


def certification_level_for(user, software: Software) -> str:
    """The tier ``user`` may certify this product at by their own standing, or ``""``.

    A one-click control has to know its own answer - there is no dropdown to ask with - so this
    is the level their standing carries rather than a level they choose.

    Vendor beats AlmaLinux where both apply, the same order ``resolve_claimed_level`` uses:
    somebody speaking for the company that makes the thing is making the stronger and more
    specific statement. Publisher or maintainer counts, as in ``can_revalidate``.
    """
    from lumina.core.certification import ValidationLevel
    from lumina.vendors.services import selectable_levels, vendors_for_submission

    behind = {pk for pk in (software.vendor_id, software.owner_vendor_id) if pk is not None}
    if vendors_for_submission(user, verified_only=True).filter(pk__in=behind).exists():
        return ValidationLevel.VENDOR
    if len(selectable_levels(user)) > 1:
        return ValidationLevel.ALMALINUX
    return ""


@transaction.atomic
def certify(*, software: Software, release: AlmaLinuxRelease, user):
    """Record that ``user``'s organisation validated this product on this major.

    The tier half of ``attest``, and deliberately the same shape: one click on the release's own
    row, no form and no queue. Routing this through the submission form instead meant opening a
    whole product form - name, publisher, description, every release - to say one thing about
    one release, and the level it carried applied to all of them.

    Who may is ``certification_level_for``; a caller that has not checked gets a refusal rather
    than a silent community row, because "validate" that quietly did something weaker would be
    worse than a button that is not there.

    ``get_or_create`` on (compatibility, level), which the database already enforces: a double
    click is an accident. ``SoftwareCertification.save`` recomputes the listing's tiers.
    """
    level = certification_level_for(user, software)
    if not level:
        raise ValueError("You cannot validate this product on behalf of anyone.")
    row = _cited_major(software, release)
    certification, created = SoftwareCertification.objects.get_or_create(
        compatibility=row, level=level, defaults={"certified_by": user},
    )
    if created:
        log_action("software.certify", target=software, actor=user,
                   after={"major": release.major, "level": level})
    return certification
