"""Hardware business logic that doesn't live on a single model.

The trust-tier and listing-ownership helpers this module used to own now live in
``lumina.vendors.services``: both are questions about a user's standing relative
to a vendor, and the software catalog needs the same answers without importing
this app.
"""
from __future__ import annotations

import re
from typing import Any

from lumina.core.certification import highest_level
from lumina.hardware.models import (
    Component,
    ComponentKind,
    HardwareListing,
    ListingEditProposal,
    Submission,
    System,
    listing_fk,
)
from lumina.vendors.services import (
    can_edit_listing,
    normalize_vendor_name,
)


def recompute_listing_levels(listing: HardwareListing) -> None:
    """Re-derive every per-release tier on ``listing``, then its rollup.

    Replaces ``upgrade_level_if_higher``, which only ever raised a listing's
    level. A tier is now *derived* from the evidence behind it, so removing an
    attestation correctly lowers the claim - the same arrangement
    ``Software.recompute_levels`` uses, and the more honest behaviour.

    Two things are deliberately different from the software version:

    - A release with no attestations gets ``""``, not the community floor. On
      hardware that row is a declaration rather than a confirmation, and the
      catalog already presents the two differently.
    - The rollup is floored at community even when every row is declared, because
      the listing-level field is non-null and drives a CSS class.

    ``attestation_count`` is the **total** across releases rather than a count of
    distinct people: one person who validates three majors has made three separate
    statements about three separate things.
    """
    versions = list(listing.versions.prefetch_related("attestations"))

    changed = []
    for version in versions:
        level = version.derived_level()
        if version.validation_level != level:
            version.validation_level = level
            changed.append(version)
    for version in changed:
        version.save(update_fields=["validation_level"])

    rollup = highest_level(
        version.validation_level
        for version in versions
        if version.validation_level
    )
    total = sum(version.attestations.count() for version in versions)

    # Written unconditionally rather than only when they differ from what
    # ``listing`` holds in memory. Callers pass instances of any age - a view's,
    # a test fixture's, one loaded before an earlier recompute already moved these
    # columns - and a "has it changed?" guard against a stale copy silently skips
    # the write, leaving the database on the old value. Two columns on one row is
    # not worth being clever about.
    listing.validation_level = rollup
    listing.attestation_count = total
    listing.save(update_fields=["validation_level", "attestation_count"])


def attach_cpu(system: System, cpu: Component) -> None:
    """Attach ``cpu`` to ``system.cpus``, enforcing that the component is
    actually a CPU. The model-level ``limit_choices_to`` only constrains the
    admin/form widgets; this is the runtime guard."""
    if cpu.kind != ComponentKind.cpu.value:
        raise ValueError(
            f"Component {cpu.pk} is kind={cpu.kind!r}, not {ComponentKind.cpu.value!r}."
        )
    system.cpus.add(cpu)


def propose_listing_edit(
    *,
    proposed_by,
    listing: HardwareListing,
    name: str = "",
    model_number: str = "",
    description: str = "",
    vendor_spec_url: str = "",
    submitter_notes: str = "",
) -> ListingEditProposal:
    """Create a pending ListingEditProposal for ``listing``.

    Permission is checked here so any caller (view, API, mgmt command) gets
    the same gate. Blank fields are allowed - they signal "no change for
    this attribute" and are filtered at approval time.
    """
    if not can_edit_listing(proposed_by, listing):
        raise PermissionError("User has no edit rights on this listing.")
    fk_kw: dict[str, Any] = (
        listing_fk(listing)
    )
    return ListingEditProposal.objects.create(
        proposed_by=proposed_by,
        name=name,
        model_number=model_number,
        description=description,
        vendor_spec_url=vendor_spec_url,
        submitter_notes=submitter_notes,
        **fk_kw,
    )


# --- duplicate detection -------------------------------------------------------
#
# The manual submit form creates listings and cannot target an existing one, which
# is what makes it safe. The cost is that nothing stops two people declaring the
# same machine: there is no duplicate check on listings (only on vendor *names*,
# in ``SubmissionForm.clean``), and ``generate_unique_slug`` quietly appends "-2",
# so the catalog forks with no warning anywhere.
#
# The run path does not have this problem - ``results.services.find_matching_system``
# matches a run to an existing listing by DMI identity, learns aliases, and routes
# vendor names through ``resolve_vendor`` specifically so that "Dell" and "Dell Inc."
# do not fork the catalog. A declared submission has no DMI identity to match on,
# only what a human typed, so the equivalent here is name and model number.
#
# Deliberately narrow. A reviewer who is shown four maybes per submission stops
# reading the warning, so this matches on equality after normalization and never on
# substrings or edit distance: "PowerEdge R750" and "PowerEdge R750xd" are different
# machines and must not be flagged as each other.

_SIMILARITY_LIMIT = 5


def _squash(text: str) -> str:
    """Lowercase and drop everything that is not a letter or digit.

    So "R-750", "R 750", and "r750" are one string. Punctuation and spacing in a
    model name carry no meaning and are exactly where hand-typed duplicates differ.
    """
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def _name_key(name: str, vendor_name: str) -> str:
    """A listing name reduced to what distinguishes it, vendor tokens removed.

    "Dell PowerEdge R750" and "PowerEdge R750" under vendor Dell are the same
    machine, and including the manufacturer in the product name is the single most
    common way two submitters describe one listing differently. ``normalize_vendor_name``
    supplies the tokens to strip, so its knowledge of corporate suffixes ("Inc",
    "Co", "Ltd") is reused rather than duplicated here.
    """
    words = re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).split()
    vendor_tokens = set(normalize_vendor_name(vendor_name).split())
    kept = [w for w in words if w not in vendor_tokens]
    # Falling back to the unstripped name matters for a listing whose name *is* the
    # vendor token, e.g. a Component named "Broadcom" under vendor Broadcom, which
    # would otherwise key to the empty string and match every other such listing.
    return "".join(kept) or "".join(words)


def _vendor_families(vendor_ids: set[int]) -> dict[int, set[int]]:
    """Map each vendor id to every vendor id naming the same company.

    Needed because the duplicate that matters most is the one under a *forked
    vendor*: the submitter proposes "Dell" inline while "Dell Inc." already exists.
    ``SubmissionForm.clean`` only rejects an exact name collision, so that inline
    vendor is created and its listing looks unrelated to Dell's by foreign key.

    One query over the vendor table, normalized in Python because the comparison is
    ``normalize_vendor_name`` and the database cannot express it. Vendors number in
    the thousands at worst, and this runs on two reviewer-only pages.
    """
    from lumina.vendors.models import Vendor

    rows = list(Vendor.objects.values_list("id", "name"))
    norm_of = {vid: normalize_vendor_name(name) for vid, name in rows}
    families: dict[str, set[int]] = {}
    for vid, norm in norm_of.items():
        families.setdefault(norm, set()).add(vid)
    return {
        vid: families.get(norm_of.get(vid, ""), {vid}) for vid in vendor_ids
    }


def _match_reason(listing: HardwareListing, other: HardwareListing) -> str:
    """Why these two look like the same hardware, or "" if they do not."""
    if _name_key(listing.name, listing.vendor.name) == _name_key(
        other.name, other.vendor.name
    ):
        return "same name"
    mine, theirs = _squash(listing.model_number), _squash(other.model_number)
    if mine and mine == theirs:
        return "same model number"
    return ""


def similar_listings(
    listing: HardwareListing, *, limit: int = _SIMILARITY_LIMIT
) -> list[tuple[HardwareListing, str]]:
    """Listings that look like the same hardware as ``listing``, newest first.

    Returns ``(other, reason)`` pairs. Both published and unpublished are included:
    two pending submissions for one machine is the case worth catching early, and it
    is invisible on the public catalog by definition.
    """
    model = type(listing)
    if listing.vendor_id is None:
        return []
    family = _vendor_families({listing.vendor_id})[listing.vendor_id]
    candidates = (
        model.objects.filter(vendor_id__in=family)
        .exclude(pk=listing.pk)
        .select_related("vendor")
        .order_by("-created_at")
    )
    found = []
    for other in candidates:
        reason = _match_reason(listing, other)
        if reason:
            found.append((other, reason))
        if len(found) >= limit:
            break
    return found


def annotate_similar_listings(submissions, *, limit: int = _SIMILARITY_LIMIT) -> None:
    """Hang ``similar_listings`` on each submission for the review queue.

    Set as an attribute rather than returned as a parallel structure, matching how
    the queue and dashboard already carry derived values (``system.user_can_edit``,
    ``product.latest_submission``); a template cannot index a dict by a
    ``(model, pk)`` pair, which is what keying this externally would require since
    System and Component pks overlap.

    Batched to keep the queue at a fixed number of queries rather than two per row.
    """
    submissions = list(submissions)
    listings = [s.listing for s in submissions if s.listing is not None]
    vendor_ids = {ls.vendor_id for ls in listings if ls.vendor_id is not None}
    for submission in submissions:
        submission.similar_listings = []
    if not vendor_ids:
        return

    families = _vendor_families(vendor_ids)
    wanted = set().union(*families.values()) if families else set()
    pool: dict[type, list] = {}
    for model in {type(ls) for ls in listings}:
        pool[model] = list(
            model.objects.filter(vendor_id__in=wanted).select_related("vendor")
            .order_by("-created_at")
        )

    for submission in submissions:
        listing = submission.listing
        if listing is None or listing.vendor_id is None:
            continue
        family = families.get(listing.vendor_id, {listing.vendor_id})
        found = []
        for other in pool.get(type(listing), ()):
            if other.pk == listing.pk or other.vendor_id not in family:
                continue
            reason = _match_reason(listing, other)
            if reason:
                found.append((other, reason))
            if len(found) >= limit:
                break
        submission.similar_listings = found


# --- why is my listing not published? ------------------------------------------
#
# The dashboard showed "unpublished" and stopped there. That is a fact about the row and
# not an answer to the question a submitter actually has, which is what to do next -
# and for some of these listings the honest answer is "nothing, this one is not yours".
#
# Three different situations were rendering identically:
#
#   1. Waiting on a reviewer. Normal, nothing to do.
#   2. Orphaned. The run that would have published it was rejected or quarantined, and
#      a rejected run is terminal (``SUBMITTABLE_STATUSES`` is draft and needs-changes),
#      so nothing about that run can ever publish it. Only a fresh passing run will.
#   3. Not the submitter's listing at all: a seeded CPU or GPU *family* that their run
#      was classified against. ``hardware/0003_reference_data.py`` seeds 81 of these
#      unpublished on purpose, and the dashboard's "my components" query matches
#      anything a user's run is linked to, so they surface as though the submitter had
#      left them half-finished. They cannot publish one and should not be asked to.

PUBLICATION_WAITING = "waiting"
PUBLICATION_BLOCKED = "blocked"
PUBLICATION_REFERENCE = "reference"
PUBLICATION_UNPROVEN = "unproven"


def publication_state(listing: HardwareListing, user=None) -> dict | None:
    """Why ``listing`` is not published yet, and what would change that.

    ``None`` for a published listing - there is nothing to explain.

    Returns ``kind`` plus a ``reason`` and a ``next_step`` in the submitter's own terms.
    Deliberately not a queryset annotation: it reads two related tables per listing and
    the dashboard caps its tables at 200 rows, so a helper called per row is the honest
    shape rather than a join that has to be maintained.
    """
    from lumina.results.models import TestRun

    if listing.published:
        return None

    # Seeded CPU and GPU *families* carry no creator. They appear in a submitter's own
    # list because the dashboard matches anything their runs are linked to, and a run is
    # linked to the family it was classified against - so the reader sees a GPU
    # generation they never heard of sitting in "my components". Reported alongside the
    # state rather than instead of it: the state still tells them what would publish it,
    # and this tells them why it is on their page.
    shared = listing.created_by_id is None or (
        user is not None and listing.created_by_id != user.pk
    )

    submissions = list(listing.submissions.all())
    open_submission = next(
        (s for s in submissions if s.status in Submission.OPEN_STATUSES), None,
    )
    if open_submission is not None:
        if open_submission.status == Submission.STATUS_NEEDS_CHANGES:
            return {
                "kind": PUBLICATION_WAITING,
            "shared": shared,
                "reason": "A reviewer asked for changes to this submission.",
                "next_step": "Revise it and it goes back in the queue.",
            }
        return {
            "kind": PUBLICATION_WAITING,
            "shared": shared,
            "reason": "Waiting for a reviewer to look at your submission.",
            "next_step": "Nothing to do - it publishes when the review is approved.",
        }

    runs = list(listing.test_runs.all()) if hasattr(listing, "test_runs") else []
    open_run = next((r for r in runs if r.status in TestRun.OPEN_STATUSES), None)
    if open_run is not None:
        return {
            "kind": PUBLICATION_WAITING,
            "shared": shared,
            "reason": "Waiting for a reviewer to look at a validation run for this.",
            "next_step": "Nothing to do - it publishes when the run is approved.",
        }
    draft_run = next(
        (r for r in runs if r.status == TestRun.STATUS_DRAFT), None,
    )
    if draft_run is not None:
        return {
            "kind": PUBLICATION_WAITING,
            "shared": shared,
            "reason": "A validation run for this has not been submitted yet.",
            "next_step": "Finish and submit that run.",
        }

    quarantined = next(
        (r for r in runs if r.status == TestRun.STATUS_QUARANTINED), None,
    )
    if quarantined is not None:
        os_name = quarantined.host_os_id or "an unrecognised OS"
        return {
            "kind": PUBLICATION_BLOCKED,
            "shared": shared,
            "reason": (
                f"The validation run for this was made on {os_name}, not AlmaLinux, "
                "so it is held for review and cannot certify anything."
            ),
            "next_step": (
                "Run the suite again on a supported AlmaLinux release and submit that."
            ),
        }
    rejected = next(
        (r for r in runs if r.status == TestRun.STATUS_REJECTED), None,
    )
    if rejected is not None:
        detail = ""
        if rejected.host_os_id and not rejected.ran_on_almalinux:
            detail = f" It was run on {rejected.host_os_id}, not AlmaLinux."
        return {
            "kind": PUBLICATION_BLOCKED,
            "shared": shared,
            "reason": (
                f"The validation run for this was rejected.{detail} A rejected run "
                "cannot be resubmitted, so nothing about it will publish this."
            ),
            "next_step": (
                "Run the suite on a supported AlmaLinux release and submit that as a "
                "new run."
            ),
        }
    if any(s.status == Submission.STATUS_REJECTED for s in submissions):
        return {
            "kind": PUBLICATION_BLOCKED,
            "shared": shared,
            "reason": "The submission for this listing was rejected.",
            "next_step": "Submit a validation run for this hardware instead.",
        }

    # Nothing of the submitter's is attached. Either a seeded reference family their run
    # was classified against, or a listing somebody else created.
    if shared:
        return {
            "kind": PUBLICATION_REFERENCE,
            "shared": shared,
            "reason": (
                "This is a shared catalog entry your run was matched against, not a "
                "listing of yours."
            ),
            "next_step": (
                "Nothing to do. It publishes when an approved, passing run proves it."
            ),
        }
    return {
        "kind": PUBLICATION_UNPROVEN,
            "shared": shared,
        "reason": "Nothing has validated this listing yet.",
        "next_step": (
            "Submit a passing validation run for it, or a submission describing it."
        ),
    }
