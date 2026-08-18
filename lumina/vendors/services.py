"""Vendor-related query helpers.

Kept separate from models.py so that view/form layers import from a service
module rather than reaching into the ORM directly - easier to mock and to
extend (e.g. adding caching) later.

``derive_allowed_levels`` and ``can_edit_listing`` live here rather than in a
catalog app because both answer questions about a user's standing *relative to a
vendor* - which vendor they may speak for, and which listings that vendor
maintains. Neither is specific to hardware, and the software catalog needs the
same answers without importing ``lumina.hardware``.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any, NamedTuple

from django.db import transaction
from django.db.models import Count, OuterRef, QuerySet, Subquery

from lumina.audit.services import log_action
from lumina.core.certification import ValidationLevel
from lumina.vendors.models import (
    Vendor,
    VendorAlias,
    VendorClaim,
    VendorMembership,
    VendorProposal,
)

# Groups whose members may certify on AlmaLinux's behalf. ``certifier`` is the
# Certification SIG: it carries this authority and nothing else, because the
# only alternative was ``admin``, which the OIDC layer escalates to
# is_staff/is_superuser. Requiring a SIG member to be a superuser of the whole
# application in order to sign off on hardware conflates certification
# authority with infrastructure authority; these are different jobs.
CERTIFYING_GROUPS = ("certifier", "admin")

# How many vendor checkboxes a filter panel renders at once.
#
# The vendor list is the one catalog facet with no upper bound - taxonomy values
# and AlmaLinux releases are curated, while vendors grow with the catalog and will
# eventually run to thousands. Rendering all of them puts a card taller than the
# results beside it into every page's HTML, so the panel takes a window and the
# search box reaches the rest.
VENDOR_FACET_LIMIT = 25


class VendorFacet(NamedTuple):
    """One catalog's vendor filter block.

    ``matched`` and ``pool`` are separate because they answer different questions.
    ``matched`` drives "showing 25 of 140" and shrinks as you type. ``pool`` ignores
    the search term and decides whether the search box exists at all - keyed off
    ``matched``, a search narrow enough to fall under the threshold would remove the
    very box you typed into, leaving no way to refine or clear it.
    """

    vendors: list[Vendor]
    matched: int
    pool: int


def vendor_facet(
    listing_model,
    *,
    selected: Sequence[str] = (),
    query: str = "",
    limit: int = VENDOR_FACET_LIMIT,
) -> VendorFacet:
    """The vendor checkboxes for a catalog's filter panel.

    Shared by both catalogs, which differ only in ``listing_model`` - they each
    offer the vendors that have at least one published listing *of that kind*, so
    a components page does not advertise vendors with only systems.

    Ordered by how many listings each vendor has, then by name. Alphabetical would
    make the window an accident of spelling; most-used-first makes a 25-row window
    worth reading.

    ``selected`` vendors are **always included**, however unpopular. Without that,
    filtering by a vendor outside the window would render a page whose own
    checkbox is missing - the filter would still be applied, with nothing on screen
    saying so and no way to switch it off.
    """
    pool_qs = Vendor.objects.published().filter(
        pk__in=listing_model.objects.filter(published=True).values("vendor")
    )
    base = pool_qs.filter(name__icontains=query) if query else pool_qs
    matched = base.count()
    # One extra count only when a term narrowed things; otherwise they are equal.
    pool = pool_qs.count() if query else matched

    # Subquery rather than Count on a reverse accessor: `vendor` is declared
    # related_name="+" on all three listing models, so there is no reverse
    # relation to aggregate over.
    per_vendor = (
        listing_model.objects.filter(published=True, vendor=OuterRef("pk"))
        .order_by()
        .values("vendor")
        .annotate(n=Count("pk"))
        .values("n")
    )
    window = list(
        base.annotate(listing_count=Subquery(per_vendor))
        .order_by("-listing_count", "name")[:limit]
    )

    if selected:
        shown = {vendor.slug for vendor in window}
        missing = [slug for slug in selected if slug not in shown]
        if missing:
            window += list(
                Vendor.objects.filter(slug__in=missing).order_by("name")
            )
    return VendorFacet(vendors=window, matched=matched, pool=pool)


def derive_allowed_levels(user, *, vendor: Vendor | None) -> list[str]:
    """Return the validation levels ``user`` may claim on a submission.

    - Unauthenticated → raises PermissionError (submissions require auth).
    - Plain users → ``[community]``.
    - ``certifier`` / ``admin`` → ``[community, almalinux]``.
    - Plus ``vendor`` when a *verified* ``vendor`` is named **and** the user may
      act for it - a submit-role member, or a certifier/admin acting for anyone.

    **The vendor tier is tied to attribution, never to standing.** It used to be
    granted for who you are: any certifier got it unconditionally, and a vendor
    member got it whether or not the submission named their vendor. That let a run
    say "vendor-validated" without saying which vendor was doing the validating,
    which is the one thing that claim has to carry. So it is now derived from
    ``vendor`` being named, and nothing else grants it.

    Order matters, but **not** as a ranking: membership is the gate, and
    ``allowed[-1]`` is the *default* when no explicit claim was made. ``vendor``
    goes last so submitting on behalf of a vendor defaults to vendor - there is no
    reason for such a run to claim anything else. With no vendor named a certifier
    defaults to ``almalinux``: a Foundation certifier validating somebody else's
    hardware is not that vendor, and their run should not say so.
    """
    if not user.is_authenticated:
        raise PermissionError("Authentication required to submit.")

    levels: list[str] = [ValidationLevel.COMMUNITY]
    certifying = user.groups.filter(name__in=CERTIFYING_GROUPS).exists()
    if certifying:
        levels.append(ValidationLevel.ALMALINUX)

    if vendor is not None and vendor.verified and (
        certifying
        or VendorMembership.objects.filter(
            user=user, vendor=vendor, role__in=VendorMembership.SUBMIT_ROLES
        ).exists()
    ):
        levels.append(ValidationLevel.VENDOR)

    return levels


def selectable_levels(user) -> list[str]:
    """The levels a submitter actually gets to *choose* between in a dropdown.

    Never includes vendor. Submitting on behalf of a vendor **is** the vendor
    claim - ``resolve_claimed_level`` sets it - so offering it as a separate
    option asks the same question twice and lets the two answers disagree. What
    remains is a real choice only for someone who may validate as the Foundation.

    A plain community member gets one entry, and callers drop the field rather
    than render a dropdown of one.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return [ValidationLevel.COMMUNITY]
    try:
        levels = derive_allowed_levels(user, vendor=None)
    except PermissionError:
        return [ValidationLevel.COMMUNITY]
    return [level for level in levels if level != ValidationLevel.VENDOR]


def resolve_claimed_level(user, *, vendor: Vendor | None, claimed: str = "") -> str:
    """The level a submission actually carries, given who and for whom.

    One function so the rule cannot differ between the four places that need it
    (the run proposal, the hardware and software submit forms, and
    ``effective_level`` at approval).

    Naming a vendor **decides** the level rather than merely permitting it: a run
    submitted on behalf of a vendor is vendor-validated, and there is no reason for
    it to say anything else. Any lower claim posted alongside a vendor is
    overridden, so the dropdown does not have to offer a choice that cannot
    sensibly be made.

    Otherwise an explicit claim is honoured if the person is entitled to it, and
    capped at their standing if not - a submission can never grant more trust than
    its submitter has.
    """
    allowed = derive_allowed_levels(user, vendor=vendor)
    if ValidationLevel.VENDOR in allowed:
        return ValidationLevel.VENDOR
    return claimed if claimed in allowed else allowed[-1]


def is_claimable(vendor: Vendor) -> bool:
    """Whether a listing should invite "are you the vendor?" for this vendor.

    Two conditions, and the second is not obvious. Unowned is the expected one:
    once somebody holds ROLE_OWNER the identity is taken. Unverified matters
    because ``derive_allowed_levels`` grants the vendor tier to any submit-role
    member of a *verified* vendor, so approving a claim on one that is already
    verified unlocks vendor-validated submissions immediately - there is no
    separate verify decision left for the reviewer to weigh, the way there is on
    an unverified vendor. A verified vendor is therefore the most valuable record
    in the system to impersonate, and its listings should not advertise the way
    in.

    This hides the invitation rather than closing the door: ``vendors:claim``
    stays reachable, because a verified vendor with no owner is a real state (the
    SIG can vouch for a company before anyone from it has an account) and a
    reviewer needs to be able to send its representative a working link.

    Shared by both catalogs' detail views so the rule cannot drift between them.
    """
    return not vendor.verified and not vendor.is_claimed


def can_edit_listing(user, listing) -> bool:
    """True if ``user`` may submit edit proposals against ``listing``.

    Edit rights are bound to listing.owner_vendor - the vendor responsible for
    keeping the listing accurate. Community-submitted listings have no owner and
    can therefore only be edited by admins via the Django admin.

    Takes any listing with an ``owner_vendor``: a System, a Component, or a
    Software product. It never looks at anything else on the object.
    """
    if not user.is_authenticated:
        return False
    if listing.owner_vendor_id is None:
        return False
    return VendorMembership.objects.filter(
        user=user,
        vendor_id=listing.owner_vendor_id,
        role__in=VendorMembership.SUBMIT_ROLES,
    ).exists()


def vendors_for_submission(user) -> QuerySet[Vendor]:
    """Vendors ``user`` may submit on behalf of (submitter or owner role).

    Accepts AnonymousUser (which has ``is_authenticated = False``) so callers
    don't need to guard the call site.
    """
    if not user.is_authenticated:
        return Vendor.objects.none()
    return Vendor.objects.filter(
        memberships__user=user,
        memberships__role__in=VendorMembership.SUBMIT_ROLES,
    ).distinct()


def can_propose_vendor_edit(user, vendor: Vendor) -> bool:
    """True when ``user`` may propose an edit to ``vendor``'s profile.

    Policy: only users with a submit-role VendorMembership. Admins can
    always go through the Django admin; we don't expose the proposal flow
    to them as the "primary" edit path.
    """
    if not user.is_authenticated:
        return False
    return VendorMembership.objects.filter(
        user=user, vendor=vendor, role__in=VendorMembership.SUBMIT_ROLES
    ).exists()


@transaction.atomic
def claim_vendor(
    *,
    vendor: Vendor,
    requester,
    work_email: str,
    role_at_vendor: str,
    note: str = "",
    evidence=None,
) -> VendorClaim:
    """Open a claim on ``vendor`` for ``requester``.

    The one-open-claim-per-requester rule is enforced **here**, not by the
    model's constraint: Django's MariaDB backend reports
    ``supports_partial_indexes = False``, so the conditional UniqueConstraint is
    skipped at migration time (system check ``models.W036``) and exists only
    under SQLite. Relying on the database would mean the rule silently held in
    tests and not in production.

    Inside a transaction with ``select_for_update`` so two simultaneous submits
    cannot both pass the check.
    """
    if not requester.is_authenticated:
        raise PermissionError("Authentication required to claim a vendor.")

    already = (
        VendorClaim.objects.select_for_update()
        .filter(
            vendor=vendor, requester=requester,
            status__in=VendorClaim.OPEN_STATUSES,
        )
        .exists()
    )
    if already:
        raise ValueError(
            f"You already have an open claim on {vendor.name}. A reviewer will "
            "get to it."
        )

    claim = VendorClaim.objects.create(
        vendor=vendor,
        requester=requester,
        work_email=work_email,
        role_at_vendor=role_at_vendor,
        note=note,
        evidence=evidence or "",
    )
    log_action("vendor_claim.submit", target=claim, actor=requester)
    return claim


def create_inline_vendor(
    *,
    name: str,
    created_by,
    scope: str = Vendor.SCOPE_HARDWARE,
    homepage: str = "",
    contact_email: str = "",
    description: str = "",
    logo=None,
) -> Vendor:
    """Materialize a vendor a submitter named inline on a submission.

    Unpublished until a reviewer approves the submission that created it, so it
    stays out of the public catalog and out of other submitters' pickers.

    The creator gets ``ROLE_SUBMITTER``, deliberately **not** ``ROLE_OWNER``.
    Typing a company's name into a form is not evidence of representing it, and
    granting ownership let a community member hold the identity of a vendor who
    had never heard of us - leaving the real vendor nothing to claim. Submit
    rights are enough to finish and edit their own listing; the identity stays
    vacant for ``VendorClaim`` to assign.
    """
    vendor = Vendor.objects.create(
        name=name.strip(),
        scope=scope,
        homepage=homepage,
        contact_email=contact_email,
        description=description,
        logo=logo or None,
        published=False,
    )
    VendorMembership.objects.get_or_create(
        user=created_by, vendor=vendor,
        defaults={"role": VendorMembership.ROLE_SUBMITTER},
    )
    return vendor


def propose_new_vendor(
    *,
    proposed_by,
    name: str,
    homepage: str = "",
    contact_email: str = "",
    description: str = "",
    logo: Any = None,
) -> VendorProposal:
    """Create a pending ``create`` VendorProposal.

    Raises ValueError on obvious duplicates (case-insensitive) so the
    review queue doesn't fill up with copies of already-listed vendors.
    """
    if Vendor.objects.filter(name__iexact=name).exists():
        raise ValueError(f"A vendor named {name!r} already exists.")
    return VendorProposal.objects.create(
        kind=VendorProposal.KIND_CREATE,
        target=None,
        proposed_by=proposed_by,
        name=name,
        homepage=homepage,
        contact_email=contact_email,
        description=description,
        logo=logo,
    )


def propose_vendor_edit(
    *,
    proposed_by,
    vendor: Vendor,
    name: str = "",
    homepage: str = "",
    contact_email: str = "",
    description: str = "",
    logo: Any = None,
) -> VendorProposal:
    """Create a pending ``update`` VendorProposal for ``vendor``."""
    if not can_propose_vendor_edit(proposed_by, vendor):
        raise PermissionError("User lacks submit-role membership for this vendor.")
    return VendorProposal.objects.create(
        kind=VendorProposal.KIND_UPDATE,
        target=vendor,
        proposed_by=proposed_by,
        name=name,
        homepage=homepage,
        contact_email=contact_email,
        description=description,
        logo=logo,
    )


# --- vendor name resolution ----------------------------------------------
# DMI strings are freeform: "Dell" / "Dell Inc." / "Dell, Inc." must all
# land on one catalog Vendor or the same machine splits across duplicates.

_VENDOR_SUFFIX_TOKENS = {
    "inc", "incorporated", "corp", "corporation", "co", "company",
    "ltd", "limited", "llc", "gmbh", "ag", "sa", "bv", "srl", "spa", "plc",
    "kg", "international", "technology", "technologies", "computer",
    "computers", "electronics", "group", "holdings",
}


def normalize_vendor_name(name: str) -> str:
    """Reduce a vendor string to its distinctive tokens, lowercased.

    "Dell, Inc." -> "dell"; "ASUSTeK COMPUTER INC." -> "asustek";
    "Micro-Star International Co., Ltd." -> "micro star".
    """
    cleaned = re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()
    tokens = [t for t in cleaned.split() if t not in _VENDOR_SUFFIX_TOKENS]
    return " ".join(tokens) or cleaned


def vendor_by_slug(slug: str):
    """The vendor whose slug is ``slug``, or None.

    A slug is not a name and must not go through ``resolve_vendor``, which tries exact names,
    aliases, then a normalized comparison. ``Vendor.save`` freezes ``slug = slugify(name)`` and
    slugify deletes in-word punctuation that ``normalize_vendor_name`` treats as a separator, so
    a single row's own slug can fail to resolve back to it: "Intel Technologies Co.,Ltd." becomes
    "intel-technologies-coltd", which normalizes to "intel technologies coltd" and matches
    nothing.

    Where that bit was reading a stored per-component claim back. An explicit tick came out as no
    vendor, and - since an explicit answer beats the default - saving the form produced a *worse*
    result than never touching it.
    """
    from lumina.vendors.models import Vendor

    if not (slug or "").strip():
        return None
    return Vendor.objects.filter(slug=slug.strip()).first()


def resolve_vendor(name: str):
    """Find the catalog Vendor a freeform manufacturer string refers to.

    Tries, in order: exact name, explicit alias, normalized comparison
    against names and aliases. Returns None when nothing matches - callers
    decide whether to create a vendor or ask a human.
    """
    from lumina.vendors.models import Vendor, VendorAlias

    if not name or not name.strip():
        return None
    name = name.strip()

    vendor = Vendor.objects.filter(name__iexact=name).first()
    if vendor:
        return vendor
    alias = (
        VendorAlias.objects.select_related("vendor")
        .filter(name__iexact=name)
        .first()
    )
    if alias:
        return alias.vendor

    wanted = normalize_vendor_name(name)
    if not wanted:
        return None
    for vendor in Vendor.objects.all():
        if normalize_vendor_name(vendor.name) == wanted:
            return vendor
    for alias in VendorAlias.objects.select_related("vendor"):
        if normalize_vendor_name(alias.name) == wanted:
            return alias.vendor
    return None


# Every concrete model carrying both a ``vendor`` FK (who makes it) and an
# ``owner_vendor`` FK (who maintains the listing). One place, because two
# functions walk it - merging a duplicate and transferring ownership on a claim -
# and a table missing from either one leaves rows pointing at a deleted vendor or
# silently un-transferred. Adding a listing type is one line here.
#
# Resolved through the app registry rather than imported, so this module never
# imports a catalog app and no circular import appears.
OWNED_LISTING_MODELS: tuple[tuple[str, str], ...] = (
    ("hardware", "System"),
    ("hardware", "Component"),
    ("software", "Software"),
)


def _owned_listing_models() -> list[type]:
    from django.apps import apps

    return [apps.get_model(app, name) for app, name in OWNED_LISTING_MODELS]


def _report_key(model: type) -> str:
    """Dict key for a model in a merge/transfer report.

    ``verbose_name_plural`` rather than the class name so the counts read as
    prose in the ``merge_vendors`` management command's summary line, and so a
    new listing type names itself instead of needing an entry in a lookup.
    """
    return str(model._meta.verbose_name_plural)


def transfer_unowned_listings(vendor: Vendor, *, by=None) -> dict[str, int]:
    """Give ``vendor`` ownership of every listing attributed to it but unowned.

    This is what makes a vendor claim worth submitting: a community member
    created listings naming Acme as the manufacturer and left ``owner_vendor``
    null, and on claim Acme becomes the maintainer of all of them at once
    instead of one reviewer action per listing.

    Listings another vendor already owns are never touched - being named as
    manufacturer does not outrank an existing maintainer.
    """
    moved: dict[str, int] = {}
    for model in _owned_listing_models():
        moved[_report_key(model)] = model.objects.filter(
            vendor=vendor, owner_vendor__isnull=True
        ).update(owner_vendor=vendor)
    if any(moved.values()):
        log_action(
            "vendor.transfer_listings", target=vendor, actor=by, after=moved,
        )
    return moved


@transaction.atomic
def merge_vendors(survivor: Vendor, duplicate: Vendor, *, by=None) -> dict:
    """Fold ``duplicate`` into ``survivor`` and leave an alias behind.

    Duplicates happen: rows created before alias resolution existed, DMI
    spelling variants, human error. The merge repoints every reference
    (listings, ownership, memberships, submissions, proposals, aliases),
    fills gaps in the survivor's profile from the duplicate, records the
    duplicate's name as a VendorAlias so the same DMI string can never
    re-create the split, and deletes the duplicate.
    """
    from lumina.hardware.models import Submission

    if survivor.pk == duplicate.pk:
        raise ValueError("Cannot merge a vendor into itself.")

    moved: dict[str, int] = {"owned": 0, "memberships": 0, "submissions": 0,
                             "proposals": 0, "aliases": 0}

    # Keyed by model name rather than a hand-written "systems"/"components" pair
    # so a new listing type appears in the report without editing this function.
    for model in _owned_listing_models():
        moved[_report_key(model)] = model.objects.filter(vendor=duplicate).update(
            vendor=survivor
        )
        moved["owned"] += model.objects.filter(owner_vendor=duplicate).update(
            owner_vendor=survivor
        )

    # memberships: move, but a user already in the survivor keeps that row
    existing_users = set(
        VendorMembership.objects.filter(vendor=survivor).values_list("user", flat=True)
    )
    dup_memberships = VendorMembership.objects.filter(vendor=duplicate)
    moved["memberships"] += dup_memberships.exclude(user__in=existing_users).update(
        vendor=survivor
    )
    dup_memberships.delete()  # whatever remains is a duplicate membership

    moved["submissions"] += Submission.objects.filter(on_behalf_of=duplicate).update(
        on_behalf_of=survivor
    )
    moved["proposals"] += VendorProposal.objects.filter(target=duplicate).update(
        target=survivor
    )
    moved["aliases"] += VendorAlias.objects.filter(vendor=duplicate).update(
        vendor=survivor
    )

    # trust and profile: merging must not reduce what either record had
    changed = []
    if duplicate.verified and not survivor.verified:
        survivor.verified = True
        changed.append("verified")
    if getattr(duplicate, "published", False) and not getattr(survivor, "published", True):
        survivor.published = True
        changed.append("published")
    for field in ("homepage", "contact_email"):
        if not getattr(survivor, field) and getattr(duplicate, field):
            setattr(survivor, field, getattr(duplicate, field))
            changed.append(field)
    if not survivor.logo and duplicate.logo:
        survivor.logo = duplicate.logo
        changed.append("logo")
    if changed:
        survivor.save(update_fields=changed)

    duplicate_name = duplicate.name
    duplicate.delete()
    VendorAlias.objects.get_or_create(
        name=duplicate_name, defaults={"vendor": survivor}
    )

    log_action(
        "vendor.merge",
        target=survivor,
        actor=by,
        after={"merged": duplicate_name, "moved": moved, "profile_changes": changed},
    )
    return moved


def represents_listing_vendor(user, listing) -> bool:
    """True if ``user`` speaks for the vendor whose hardware this listing is.

    Wider than ``can_edit_listing`` on purpose, and the difference matters. Edit rights
    are bound to ``owner_vendor``, the vendor that has taken responsibility for keeping a
    listing accurate - and a listing the community created has no owner at all, so a Dell
    engineer would fail that test on a Dell machine somebody else catalogued first.

    This asks the question a submitter actually poses: "is this my company's hardware?"
    So membership in either the manufacturer (``vendor``) or the maintainer
    (``owner_vendor``) counts, with the same submit roles ``can_edit_listing`` uses.
    """
    if not getattr(user, "is_authenticated", False):
        return False
    vendor_ids = {
        vid for vid in (listing.vendor_id, listing.owner_vendor_id) if vid is not None
    }
    if not vendor_ids:
        return False
    return VendorMembership.objects.filter(
        user=user, vendor_id__in=vendor_ids, role__in=VendorMembership.SUBMIT_ROLES,
    ).exists()
