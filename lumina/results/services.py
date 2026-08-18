"""Review actions and publication logic for ingested runs.

Kept out of the views so the review UI, the API, and management commands all
go through identical rules and produce identical audit entries.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from lumina.audit.services import log_action
from lumina.core.certification import ValidationLevel, level_outranks
from lumina.hardware.models import (
    CommunityAttestation,
    ComponentKind,
    ListingVersion,
    System,
    listing_fk,
)
from lumina.hardware.services import recompute_listing_levels
from lumina.results import inventory_extract, proposal_keys
from lumina.results.inventory_extract import is_placeholder
from lumina.results.models import (
    ReportedIdentityAlias,
    ResultStatus,
    RunType,
    Severity,
    SystemKind,
    TestRun,
)
from lumina.results.pci_names import gpu_identity, nic_identity
from lumina.vendors.models import Vendor, VendorAlias
from lumina.vendors.services import (
    represents_listing_vendor,
    resolve_claimed_level,
    resolve_vendor,
    vendor_by_slug,
)

# Statuses from which a submitter may (re)send a run to the reviewers. A run
# the reviewer bounced back belongs here: "needs changes" is a request to the
# submitter, so it has to be actionable by them.
SUBMITTABLE_STATUSES = (TestRun.STATUS_DRAFT, TestRun.STATUS_NEEDS_CHANGES)


def find_matching_system(vendor_name: str, product: str):
    """The existing System listing a run's DMI identity refers to, if any.

    A recorded alias is consulted first: once a human has worked out that
    vendor "OEM" product "7D2XCTO1WW" is a ThinkSystem SR645, the next run of
    that machine must not ask again. Without it, unhelpful firmware strings
    match nothing every time and each submitter re-derives the answer, possibly
    differently.

    Failing that, the name is matched directly, with vendors going through
    ``resolve_vendor`` so "Dell" and "Dell Inc." land on the same listing
    instead of forking the catalog.
    """
    aliased = ReportedIdentityAlias.resolve(vendor_name, product)
    if isinstance(aliased, System):
        return aliased
    if not vendor_name or not product:
        return None
    vendor = resolve_vendor(vendor_name)
    if vendor is None:
        return None
    return System.objects.filter(vendor=vendor, name__iexact=product.strip()).first()


def existing_listing_for(run: TestRun):
    """The catalog listing this run is a *re-validation* of, or None for new hardware.

    Linked first, because ``auto_link_existing_system`` sets that at ingest. Otherwise
    resolved the same way the run-detail prompt resolves it, so a run that matches an
    existing listing but has not been linked yet counts as a re-validation rather than as
    an opportunity to describe the machine again.

    A custom build is identified by its motherboard, so that is the listing for this
    purpose - the same rule ``missing_submission_details`` applies.
    """
    from lumina.hardware.models import ComponentKind as _ComponentKind

    # A disputed identity makes this new hardware, whatever was matched or linked before. Every
    # caller keys off this - the gate, the identity fields, the attribution list - so one flag
    # turns the whole page back into "describe this machine".
    if run.identity_disputed:
        return None
    if run.listing_system_id:
        return run.listing_system
    kind = run.effective_system_kind
    if kind == SystemKind.PREBUILT:
        return resolve_reported_system(run)
    if kind == SystemKind.CUSTOM:
        board = run.listing_components.filter(
            kind=_ComponentKind.motherboard.value
        ).first()
        return board or find_matching_board(run.board_vendor, run.board_model)
    return None


def identity_vendors(run: TestRun) -> list:
    """The vendors whose *machine* this is.

    Whose certification a run could sensibly be. A machine's identity belongs to whoever
    made it: the system manufacturer for a prebuilt, the board manufacturer for a custom
    build, and the catalog listing's own vendor once the run is linked to one.

    Not the makers of the parts inside it: certifying the machine is a claim about the whole
    machine, which only its manufacturer is positioned to make. Those are
    ``attributable_vendors`` below, which is a wider question - Intel can validate their own
    CPU inside somebody else's chassis, they just cannot certify the chassis.

    This is the narrow set, used to decide which vendor to *preselect*.

    Empty when nothing resolves, which is a real case rather than a failure: brand-new
    hardware from a vendor the catalog has never seen reports a manufacturer string that
    matches nothing yet. Callers treat empty as "cannot tell" rather than "nobody", because
    restricting on that guess would lock a new vendor out of submitting their own first
    machine.
    """
    from lumina.results.models import SystemKind

    found: list = []

    def add(vendor) -> None:
        if vendor is not None and not any(v.pk == vendor.pk for v in found):
            found.append(vendor)

    reported = (
        run.system_vendor
        if run.effective_system_kind == SystemKind.PREBUILT
        else run.board_vendor
    )
    if reported:
        add(resolve_vendor(reported))
    # A linked listing settles it: a human or the identity matcher has already decided which
    # catalog entry this machine is, and that entry names its manufacturer.
    listing = existing_listing_for(run)
    if listing is not None:
        if listing.vendor_id:
            add(listing.vendor)
        if listing.owner_vendor_id:
            add(listing.owner_vendor)
    return found


def attributable_vendors(run: TestRun) -> list:
    """Every vendor a run could sensibly be attributed to.

    The machine's own makers, plus the makers of the parts it exercised. Intel validating
    their CPU inside a partner's chassis is a real thing they want to state, and the run is
    genuine evidence about that CPU - so Intel belongs in the attribution list even though
    the chassis is not theirs.

    Safe to be this wide only because ``effective_level`` caps the vendor tier per listing:
    an Intel-attributed run gets ``vendor`` on Intel's CPU family and community on the Dell
    system, rather than Intel certifying Dell's hardware. Widening this without that cap
    would be the same bug in reverse, so the two arrived together.
    """
    found = list(identity_vendors(run))
    seen = {vendor.pk for vendor in found}
    for target in component_tie_targets(run):
        vendor = resolve_vendor(target["brand"])
        if vendor is not None and vendor.pk not in seen:
            seen.add(vendor.pk)
            found.append(vendor)
    return found


def reported_identities(run: TestRun) -> list[tuple[str, str]]:
    """The (vendor, model) pairs a run reports that could identify the machine.

    System table first, board second. A machine whose system table names nothing
    is identified by its board, and that is exactly the case an alias is for -
    so the board string has to be a candidate even on a run the submitter has
    declared to be a vendor system.
    """
    return run.reported_identity_pairs


def apply_alias_kinds(runs) -> list:
    """Pre-resolve corrected machine kinds for a list of runs, in one query.

    ``effective_system_kind`` consults the alias table, so a feed of runs would
    otherwise be a query per row. The table holds one row per hardware identity
    a human has ruled on, so loading the ones carrying a kind is small.
    """
    runs = list(runs)
    if not runs:
        return runs
    lookup = {
        ((vendor or "").strip().lower(), (product or "").strip().lower()): kind
        for vendor, product, kind in ReportedIdentityAlias.objects
        .exclude(resolved_kind="")
        .values_list("reported_vendor", "reported_product", "resolved_kind")
    }
    for run in runs:
        run._alias_kind = ""
        for vendor, product in run.reported_identity_pairs:
            found = lookup.get(
                ((vendor or "").strip().lower(), (product or "").strip().lower())
            )
            if found:
                run._alias_kind = found
                break
    return runs


def sibling_runs(run: TestRun, statuses):
    """The submitter's other validation runs making the same claim about the same hardware.

    "Same hardware" is the reported identity, matched case-insensitively on
    either the system table or the board, so it holds across firmware
    capitalization changes. Restricted to the same submitter: somebody else's
    run of the same model is an independent submission, not part of this batch.

    **And the same claim, which the identity does not imply.** A host can be the subject of a
    whole-machine run and merely the venue of a GPU-scoped one, and those are two submissions that
    happen to share a chassis. Grouping them did real damage in both directions: the listing details
    a submitter typed for the machine were copied onto the scoped run through
    ``merge_listing_proposal``, giving a component claim a submitter-asserted machine name, and
    ``approve_group`` swept the scoped run through review at the machine's level under the heading
    "Submit all N runs of this machine".

    Compared as stored, which is why ingest sorts and de-duplicates ``claim_scope``: an exact match
    is only meaningful against a canonical form.
    """
    from django.db.models import Q

    identity = Q()
    if (run.system_product or "").strip():
        identity |= Q(system_vendor__iexact=run.system_vendor or "",
                      system_product__iexact=run.system_product)
    if (run.board_model or "").strip():
        identity |= Q(board_vendor__iexact=run.board_vendor or "",
                      board_model__iexact=run.board_model)
    if not identity:
        return TestRun.objects.none()
    return (
        TestRun.objects.filter(
            identity,
            submitter=run.submitter,
            run_type=RunType.validate.value,
            claim_scope=sorted(set(run.claim_scope or [])),
            status__in=statuses,
            # Never an archived one. "Submit all N runs of this machine" is a convenience, and
            # sweeping up a draft the submitter deliberately put away would submit work they had
            # said they were not taking further - and do it without naming it, since an archived
            # run is by definition not on the page they are looking at.
            archived_at=None,
        )
        .exclude(pk=run.pk)
        .select_related("alma_release")
    )


def sibling_draft_runs(run: TestRun):
    """The submitter's other *unsubmitted* runs of the same reported hardware.

    Listing details describe a machine, not a run, so a submitter who uploads
    9.6, 10.2 and 8.10 back to back should answer once. Before this they were
    asked three times, and any variation in what they typed - "PowerEdge R7715"
    versus "Dell PowerEdge R7715" - forked the catalog into duplicate listings,
    because nothing exists to auto-link against until the first approval.

    Drafts only. A run the reviewer sent back carries a specific request, and
    overwriting its answer from a sibling would discard whatever was asked for.
    """
    return sibling_runs(run, [TestRun.STATUS_DRAFT])


def pending_sibling_runs(run: TestRun):
    """The other runs of this machine waiting in the review queue."""
    return sibling_runs(run, [TestRun.STATUS_PENDING])


def approve_group(run: TestRun, *, by, notes: str = "", actor=None) -> tuple[list, list]:
    """Approve this run and the machine's other pending runs.

    A vendor submits one machine on several AlmaLinux releases, so a reviewer
    reading that machine's evidence had to open and approve each run separately.
    The listing details are identical by construction - they are shared across
    the batch - so the only difference between the runs is which release they
    passed on.

    Returns ``(approved, blocked)`` with blocked as ``(run, reason)``. A sibling
    that did **not** pass is never swept in: the reviewer is looking at this
    run's results, has not seen that one's failures, and approving it here would
    record a decision nobody made. Same for anything ``approve_run`` refuses.
    """
    approved, blocked = [], []
    for member in [run, *pending_sibling_runs(run)]:
        if member.pk != run.pk:
            if member.verdict() is not True:
                blocked.append((
                    member,
                    "it did not pass, so it needs reviewing on its own page",
                ))
                continue
            # The reviewer read one page and pressed one button, so the answers they gave there
            # are answers about the machine, not about the run that happened to be on screen.
            # Without this, unticking "Certify as Intel" and approving all four runs declined the
            # claim on one and granted it on three - the same silent grant the shared form exists
            # to stop, just moved one button along.
            #
            # After the verdict check, not before. A blocked run is one the reviewer is being told
            # to open on its own page, and writing this run's answers onto it first would mean
            # arriving at a decision they never made about a run they have not seen.
            merge_component_answers(run, member)
            member.save(update_fields=["component_overrides", "excluded_component_ties"])
            # Logged, because this is a write to a run nobody opened. Every diagnosis of a wrong
            # tier in this system has started from the audit trail, and an unexplained change on
            # a page the reviewer never visited is the hardest kind to account for later.
            log_action(
                "test_run.component_ties_shared", target=member, actor=actor or by,
                after={"from_run": str(run.pk),
                       "overrides": member.component_overrides,
                       "excluded": member.excluded_component_ties},
            )
        try:
            approve_run(member, by=by, notes=notes)
        except ReviewError as exc:
            blocked.append((member, str(exc)))
        else:
            approved.append(member)
    return approved, blocked


def _claimed_majors(blob: dict | None) -> set[int]:
    """The AlmaLinux majors a proposal blob claims.

    Majors only. This returned ``{major: minimum_minor}`` and carried a floor per major, which
    hardware no longer certifies against - see ``ListingVersion.display``. Legacy blobs still
    hold ``release_minor_*`` keys from before the change; ``proposal_keys.release_major`` refuses
    them, so they read as nothing rather than as a claim on some major named "minor".
    """
    claimed: set[int] = set()
    for key, value in (blob or {}).items():
        if not value:
            continue
        major = proposal_keys.release_major(key)
        if major is not None:
            claimed.add(major)
    return claimed


def merge_listing_proposal(previous: dict | None, incoming: dict | None) -> dict:
    """Combine two proposal blobs so AlmaLinux support is only ever **added**.

    Everything except the release selection is a *correction* - a description, a
    model number, a CPU family can all be wrong and need fixing - so ``incoming``
    wins there. The release selection is a *claim*, and replacing the blob was
    silently retracting claims two ways:

    - unticking a box removed a major the submitter had already stated, and
    - a major that stopped being ``supported()`` vanishes from the form entirely,
      so the next save of *any* field dropped it without anyone touching it.

    So majors union. The minor floor that used to travel with them is gone: hardware certifies
    per major, and the "lower floor wins" rule went with it.

    Legacy ``release_minor_*`` keys are dropped as blobs pass through here, so a run saved once
    after the change stops carrying them. Nothing reads them, but leaving them in would keep
    them turning up in the audit log and on the review page for years.

    The consequence worth knowing: a submitter cannot take a release back here.
    That is deliberate. Retracting a support claim is a reviewer's decision, not a
    side effect of editing the description.
    """
    merged = {
        key: value for key, value in (incoming or {}).items()
        if not key.startswith(proposal_keys.RELEASE_MINOR_PREFIX)
    }
    for major in sorted(_claimed_majors(previous) | _claimed_majors(incoming)):
        merged[proposal_keys.release_key(major)] = True
    return merged


def _listing_majors(listing) -> dict[int, str]:
    """``{major: validation_level}`` for the releases a listing already records."""
    if listing is None:
        return {}
    return {
        version.release.major: version.validation_level
        for version in listing.versions.select_related("release")
    }


def _other_claimed_majors(run: TestRun, proposal: dict) -> set[int]:
    """Claimed majors other than the one this run proves.

    The proven release is evidence and is reported on its own; repeating it among the rest is
    what made a single "claimed" row ambiguous.
    """
    claimed = set(_claimed_majors(proposal))
    if run.alma_release_id is not None:
        claimed.discard(run.alma_release.major)
    return claimed


def _carried_releases(run: TestRun, listing, proposal: dict) -> list[dict]:
    """Ticked majors the listing already records, with the tier it records for them."""
    known = _listing_majors(listing)
    carried = []
    for major in sorted(_other_claimed_majors(run, proposal)):
        if major not in known:
            continue
        level = known[major]
        carried.append({
            "major": major, "level": level,
            # The label, not the stored slug: "community" is a database value and this row is
            # read by a person.
            "level_display": dict(ValidationLevel.choices).get(level, ""),
        })
    return carried


def _new_declared_releases(run: TestRun, listing, proposal: dict) -> list[dict]:
    """Ticked majors the listing does not record yet."""
    known = _listing_majors(listing)
    return [
        {"major": major}
        for major in sorted(_other_claimed_majors(run, proposal))
        if major not in known
    ]


def _incoming_level(run: TestRun, listing, vendor_name: str) -> str:
    """The tier this run's evidence carries for the listing the review box is about.

    Asked for directly: the "Proves" row named the release but not the tier, which left the
    reviewer to work out from the dropdown below what the evidence would actually count as - and
    the dropdown is a ceiling, not the answer.

    An existing listing has one, so ``run_trust_level`` gives it: the frozen attestation where
    there is one, else the live derivation. Deliberately the same helper the run's evidence
    display uses, so the tier a reviewer reads before approving is the one they see after.

    A listing being *created* has no row yet, so the tier is derived from the vendor it will
    have. The vendor claim holds only when the submitter attributed the run to that same vendor:
    the rule ``create_listings_from_run`` applies when it sets ``owner_vendor``, and the one
    ``_listing_belongs_to`` checks afterwards. Mirrored rather than shared because the listing
    does not exist yet, and pinned by ``test_the_incoming_tier_matches_what_approval_records``
    so the mirror cannot drift.
    """
    if listing is not None:
        return run_trust_level(run, listing)

    prospective = resolve_vendor(vendor_name) if vendor_name else None
    attributed = (
        run.on_behalf_of
        if run.on_behalf_of_id and prospective is not None
        and prospective.pk == run.on_behalf_of_id
        else None
    )
    return resolve_claimed_level(
        run.submitter, vendor=attributed, claimed=run.claimed_validation_level,
    )


def _parts_capped_below(run: TestRun, level: str) -> list[dict]:
    """Attached parts whose tier will be lower than the listing's, with what they get.

    The other half of the answer to "components? only the system?": a vendor tier says the
    company that makes a thing validated it, so it stops at the parts that company makes. Naming
    the parts that fall back beats a sentence saying it can happen.
    """
    capped = []
    for component in run.listing_components.all():
        theirs = run_trust_level(run, component)
        if level_outranks(level, theirs):
            capped.append({
                "label": str(component),
                "level": theirs,
                "level_display": dict(ValidationLevel.choices).get(theirs, ""),
            })
    return capped


def proposal_effect(run: TestRun) -> dict:
    """What approving this run would do to the catalog, as plain data for a reviewer.

    The review page used to render the proposal blob's ``vendor_name`` and ``name`` straight,
    with copy saying "approving this run creates the System listing with these details". On a
    re-validation of hardware already in the catalog both are wrong: those keys are not in the
    blob at all - the identity is locked for anyone who does not speak for the listing's vendor,
    so the values are discarded rather than stored - and approving reuses the existing listing
    rather than creating anything. Reported as the box being broken, and it rendered two labels
    with nothing after them.

    What is in the blob for such a run is what the reviewer actually needs to weigh: the
    AlmaLinux support being claimed and any CPU correction. Neither was shown anywhere.

    Derived the same way ``create_listings_from_run`` derives it, so the two cannot disagree
    about what approval will use: the submitter's answer where they gave one, the report's own
    strings otherwise.
    """
    proposal = run.listing_proposal or {}
    listing = existing_listing_for(run)
    from_report = not (proposal.get("vendor_name") or proposal.get("name"))

    # A scoped run's effect is on its parts, and every field below describes a machine. Left
    # unguarded this told a reviewer "This run is evidence about Dell OptiPlex 3080" and, on a host
    # nothing had catalogued, that approving would create a System listing for it. Neither is a thing
    # approval does: ``create_listings_from_run`` takes its scoped branch straight to
    # ``ensure_component_ties``, and ``scoped_listings`` excludes the host from everything downstream.
    if run.is_scoped:
        # ``preview_component_ties`` rather than ``component_tie_targets``: it is the same
        # resolution approval performs, reported rather than applied, and it carries the name a part
        # would actually be catalogued under. The raw strings would name the die and bracket the
        # product - "CometLake-S GT2 [UHD Graphics 630]" - which is not what approving creates.
        parts = [
            entry["catalog_name"] or entry["raw_model"]
            for entry in preview_component_ties(run)
            if not entry["excluded"]
        ]
        return {
            "scoped": True,
            "scope_labels": run.scope_labels,
            # No System, ever. Named as its own key rather than left to ``creates: False``, which a
            # reader would take to mean the machine is already listed.
            "creates": False,
            "creates_system": False,
            "listing": None,
            "host_name": run.host_name,
            # The parts approval will tie and attest, which is the whole effect.
            "parts": parts,
            "level": _incoming_level(run, None, ""),
            "level_display": dict(ValidationLevel.choices).get(
                _incoming_level(run, None, ""), ""
            ),
            # A claim with no gating result certifies nothing, and the reviewer has to know that
            # before approving rather than afterwards. ``verdict()`` cannot say so: it answers "did
            # anything fail", and is True when nothing ran at all.
            "unevidenced": unevidenced_claims(run),
            "run_release": (
                {"major": run.alma_release.major, "minor": run.alma_minor}
                if run.alma_release_id is not None else None
            ),
        }

    if run.system_kind == SystemKind.CUSTOM:
        reported_vendor, reported_name = run.board_vendor, run.board_model
    else:
        reported_vendor, reported_name = run.system_vendor, run.system_product

    level = _incoming_level(run, listing, proposal.get("vendor_name") or reported_vendor or "")

    return {
        # Approving creates a listing only when nothing already covers this machine.
        "creates": listing is None,
        "listing": listing,
        "vendor": proposal.get("vendor_name") or reported_vendor or "",
        "name": proposal.get("name") or reported_name or "",
        # Whether the identity above is the submitter's answer or the firmware's, which decides
        # whether a reviewer is reviewing a claim or a reading.
        "from_report": from_report,
        "model_number": proposal.get("model_number") or run.system_model_number or "",
        "description": proposal.get("description") or "",
        "vendor_spec_url": proposal.get("vendor_spec_url") or "",
        # The correction, only when it differs from what the collector reported.
        "cpu_model": (
            submitted_cpu_model(run)
            if submitted_cpu_model(run) != (run.cpu_model or "") else ""
        ),
        # What the evidence counts as for this listing, and which parts do not get that much.
        "level": level,
        "level_display": dict(ValidationLevel.choices).get(level, ""),
        "parts_capped": _parts_capped_below(run, level),
        # The release this run itself passed on, which is the only one it is evidence for and
        # the only version row that can gain a tier from approving it.
        "run_release": (
            {"major": run.alma_release.major, "minor": run.alma_minor}
            if run.alma_release_id is not None else None
        ),
        # Support the listing **already** records, carried forward untouched.
        #
        # Reported: "'Also ticked' implies 'we' ticked it. It was already set and we're simply
        # carrying it forward. It's existing implied support that we are not modifying in any
        # way." Exactly right, and the page said the opposite twice over - it credited the tick
        # to the submitter, then warned it would not be recorded, which reads as a loss when
        # there is nothing to record.
        #
        # The tick is not an assertion at all here. ``claimed_release_ticks`` pre-ticks whatever
        # the listing and the submitter's other runs already claim, precisely so that saving the
        # form does not look like retracting them.
        "carried": _carried_releases(run, listing, proposal),
        # Majors that are genuinely new: ticked, not proven by this run, and not already on the
        # listing. These are a real declaration, and ``apply_proposal_metadata`` writes them only
        # when approval *creates* the listing - so on a re-validation they are dropped, which is
        # a gap rather than a wording problem.
        "new_declarations": _new_declared_releases(run, listing, proposal),
        # Whether a new declaration would be recorded at all.
        "declares": listing is None,
        # Maintenance fields only land on an existing listing for somebody who speaks for its
        # vendor (``apply_owner_maintenance``), and never touch its identity. Saying so beats a
        # reviewer wondering why a description they can see is not going to appear.
        "applies_details": listing is None or represents_listing_vendor(run.submitter, listing),
    }


def claimed_release_ticks(run: TestRun) -> dict:
    """Every AlmaLinux release already claimed for this machine, as form ticks.

    The gap this closes: a new run's form only ever borrowed from *draft* siblings,
    so the moment a run was sent for review or approved its release selection
    became invisible. Upload a run on 10 after having declared 8, 9 and 10, and the
    form came back with only 10 ticked - which reads as the boxes being unchecked,
    and saving made it true.

    Two durable sources, neither of which depends on a run's status:

    - the **catalog listing**, whose ``ListingVersion`` rows are the real record of
      what this machine claims; and
    - the submitter's other runs of the same machine, whatever state they are in.

    Majors only. This carried a minor floor and took the lowest where the sources disagreed;
    hardware no longer certifies against a minor, so there is nothing to reconcile.
    """
    majors: set[int] = set()

    listing = run.listing_system or resolve_reported_system(run)
    if listing is not None:
        majors.update(
            version.release.major
            for version in listing.versions.select_related("release")
        )

    # Any status, deliberately. A submitted or approved run of this machine still
    # represents what its submitter said the machine supports.
    for sibling in sibling_runs(run, [status for status, _ in TestRun.STATUS_CHOICES]):
        majors.update(_claimed_majors(sibling.listing_proposal))

    return {proposal_keys.release_key(major): True for major in sorted(majors)}


def _tie_identities(run: TestRun) -> dict:
    """``{tie key: (model name, pk)}`` for the parts on ``run`` that resolve to a catalog listing.

    The bridge between two runs of one machine. A tie key is built from the *reported* model
    string, so the same physical part can be filed under different keys on two runs when the
    firmware spells it differently on different releases, or when one run carries a correction the
    other does not. The resolved component is the same object either way, and that is what an
    answer about the part is really about.
    """
    identities = {}
    for entry in preview_component_ties(run):
        component = entry.get("component")
        if component is not None:
            identities[entry["key"]] = (type(component).__name__, component.pk)
    return identities


def merge_component_answers(source: TestRun, target: TestRun) -> None:
    """Apply ``source``'s per-component answers to ``target``. No save; the caller saves, because
    the two callers save different field sets.

    Both places that apply one run's decisions to the machine's other runs need this, and for the
    same reason: an unanswered claim box is offered ticked, so a decline that fails to travel is
    not a neutral loss - the sibling silently certifies at the vendor tier somebody just declined.

    Two rules, and they are the whole function:

    **Matched by the part, not by the string.** Keys are translated through the resolved component
    where both runs resolve one, so an answer about the Intel CPU family reaches the sibling even
    though its report spells the model differently. Keyed on the raw string alone, a decline
    quietly failed to travel in exactly the case it was needed. Where a part resolves to no
    component the key is copied verbatim, which is inert: an unresolved part cannot carry a vendor
    claim in the first place.

    **On a disagreement the more restrictive answer wins.** A target that recorded its own answer
    keeps it, except that a decline coming in overrides a claim already there. This started as a
    plain ``dict.update``, which let the source win every subkey - so approving a group from a run
    whose box was merely *offered* ticked overwrote a sibling's explicitly saved decline and
    certified the part at the vendor tier somebody had turned down. Silence must never outrank an
    answer, and a bulk action must never grant more trust than the reader asked for. Exclusion is
    a union for the same reason: dropping a part is the restrictive answer, so it travels, and a
    re-inclusion does not.
    """
    if target.component_overrides is None:
        target.component_overrides = {}
    source_identities = _tie_identities(source)
    target_keys = {entry["key"] for entry in preview_component_ties(target)}
    by_identity = {
        identity: key for key, identity in _tie_identities(target).items()
    }

    def _target_key(key: str) -> str:
        if key in target_keys:
            return key
        identity = source_identities.get(key)
        return by_identity.get(identity, key) if identity else key

    for source_key, chosen in (source.component_overrides or {}).items():
        if not isinstance(chosen, dict):
            continue
        current = target.component_overrides.setdefault(_target_key(source_key), {})
        for subkey, value in chosen.items():
            if subkey not in current:
                current[subkey] = value
            elif subkey == "attribute_to" and not value:
                current[subkey] = ""
    target.excluded_component_ties = sorted(
        set(target.excluded_component_ties or [])
        | {_target_key(key) for key in (source.excluded_component_ties or [])}
    )


def archive_run(run: TestRun, *, by) -> TestRun:
    """Put a run out of sight on its submitter's dashboard.

    Refuses anything outside ``ARCHIVABLE_STATUSES``, which is the whole safety story: a pending
    run is in a reviewer's queue and an approved one is evidence, so neither can be hidden by the
    person who submitted it.

    Reversible, and logged. Archiving destroys nothing and is a statement about one person's view
    of their own work, but "where did that run go" is a question somebody will ask, and the audit
    trail is how every other question like it has been answered in this system.
    """
    if run.submitter_id != getattr(by, "pk", None):
        raise ReviewError("Only the submitter can archive their own run.")
    if not run.can_archive:
        raise ReviewError(
            f"A {run.get_status_display().lower()} run cannot be archived."
            if run.status not in TestRun.ARCHIVABLE_STATUSES
            else "That run is already archived."
        )
    run.archived_at = timezone.now()
    run.save(update_fields=["archived_at"])
    log_action("test_run.archive", target=run, actor=by,
               after={"archived_at": run.archived_at.isoformat(), "status": run.status})
    return run


def unarchive_run(run: TestRun, *, by) -> TestRun:
    """Bring it back. No status check: whatever could be archived can come back, and a run whose
    status changed while it was away is a case that cannot arise, because nothing else touches an
    archived run."""
    if run.submitter_id != getattr(by, "pk", None):
        raise ReviewError("Only the submitter can restore their own run.")
    if not run.is_archived:
        raise ReviewError("That run is not archived.")
    run.archived_at = None
    run.save(update_fields=["archived_at"])
    log_action("test_run.unarchive", target=run, actor=by, after={"status": run.status})
    return run


def share_listing_details(run: TestRun) -> list[TestRun]:
    """Copy this run's answers onto the submitter's other drafts of the machine.

    Returns the runs updated, so the UI can say what it did rather than
    silently changing pages the submitter is not looking at.

    Merged rather than copied: a sibling may already claim a release this run does
    not, and "these details were applied to your other runs" must not mean a
    release quietly disappeared from one of them.
    """
    siblings = list(sibling_draft_runs(run))
    for sibling in siblings:
        sibling.listing_proposal = merge_listing_proposal(
            sibling.listing_proposal, run.listing_proposal
        )
        sibling.on_behalf_of_id = run.on_behalf_of_id
        sibling.claimed_validation_level = run.claimed_validation_level
        # The per-component answers travel too, and they have to.
        #
        # They did not, while the page said "these details were applied to your other runs, so
        # you do not have to enter them again". A submitter who unticked "Certify as Intel" and
        # then used "Submit all N runs of this machine" had that decline apply to the run they
        # were looking at and to none of the others - which, now that an unanswered box is
        # honoured as a claim, silently certifies the sibling at the vendor tier they had just
        # declined. That multi-release submit is the ordinary path, not a corner.
        merge_component_answers(run, sibling)
        # submitter_notes stay per-run: they are about this run, not the machine.
        sibling.save(update_fields=["listing_proposal", "on_behalf_of",
                                    "claimed_validation_level",
                                    "component_overrides",
                                    "excluded_component_ties"])
    return siblings


def resolve_reported_alias(run: TestRun):
    """The alias row matching anything this run reports, or None."""
    for vendor, product in reported_identities(run):
        alias = ReportedIdentityAlias.for_identity(vendor, product)
        if alias is not None:
            return alias
    return None


def resolve_reported_system(run: TestRun):
    """The System a run's reported identity points at, aliases included."""
    alias = resolve_reported_alias(run)
    if alias is not None and isinstance(alias.listing, System):
        return alias.listing
    # Not gated on the run looking prebuilt: this matches the *system* table's
    # own vendor and model, so if a System listing carries those strings it is
    # that machine whatever the kind heuristic decided. Gating it meant a
    # vendor that mirrors its system name into the baseboard - HP does, on the
    # ProLiant line - was classified custom and then never auto-linked, even
    # though its own strings matched a listing exactly.
    return find_matching_system(run.system_vendor, run.system_product)


def auto_link_existing_system(run: TestRun) -> bool:
    """Link a run to an already-cataloged System, unless its identity is disputed.

    Guarded here rather than at each call site because there are two, and the second one is
    easy to miss: ``submit_for_review`` re-links so that a catalog entry which arrived after
    ingest can still be picked up. That re-link silently undid a dispute - the submitter said
    "this is not that machine", and submitting the run reattached it on the way to review.

    Keeps re-validations of known hardware zero-effort for the submitter -
    they only get asked for listing details when the model is genuinely new.

    Not restricted to prebuilt-looking runs any more: a recorded alias can map
    an unhelpful board string to a System, and that mapping exists precisely
    because the firmware does not present the machine as a vendor system.
    """
    if run.identity_disputed:
        return False
    if run.listing_system_id:
        return False
    # A scoped run has no machine to attach, and attaching one anyway is how the host got into
    # the catalog. Reproduced with EC2 DMI: a GPU-scoped run created ``System(Amazon EC2
    # m5.large)``, and then a *second tenant's* GPU-scoped run of the same instance type matched
    # it here and arrived at review already bound to it with nothing outstanding, so the reviewer
    # was asked to approve a card against somebody else's rented instance.
    #
    # An instance type is not a machine. It is shared by every tenant, the next boot is different
    # silicon, and nobody can certify it by validating a card that happened to be passed through.
    if run.is_scoped:
        return False

    alias = resolve_reported_alias(run)
    if alias is not None:
        # Carry the kind correction forward. Stored in listing_proposal, which
        # is the existing override channel, so system_kind stays as the raw
        # evidence of what the firmware said and effective_system_kind picks
        # this up with no extra query.
        if alias.resolved_kind and not (run.listing_proposal or {}).get("machine_kind"):
            proposal = dict(run.listing_proposal or {})
            proposal["machine_kind"] = alias.resolved_kind
            run.listing_proposal = proposal
            run.save(update_fields=["listing_proposal"])
        if not isinstance(alias.listing, System):
            # A board mapping links the component instead, so the run arrives
            # linked either way and is not sent back to the submitter.
            if alias.listing is not None:
                run.listing_components.add(alias.listing)
                return True
            return False

    system = resolve_reported_system(run)
    if system is None:
        return False
    run.listing_system = system
    run.save(update_fields=["listing_system"])
    return True


# lscpu reports the CPUID vendor string; the catalog wants the brand.
CPU_VENDOR_NAMES = {
    "authenticamd": "AMD",
    "genuineintel": "Intel",
    "arm": "Arm",
    "apm": "Ampere",
}
# The suite reports GPU vendors as lowercase PCI-id names.
def _vendor_for(name: str) -> Vendor:
    """Resolve a freeform vendor string to a Vendor, creating one only when
    no existing vendor or alias matches - "Dell" must not fork "Dell Inc."."""
    vendor = resolve_vendor(name)
    if vendor is None:
        vendor = Vendor.objects.create(name=name.strip())
    return vendor


def silicon_component(vendor, raw_model, kind, **kw):
    """The catalog entry a reported CPU/GPU string belongs to.

    Certification is granted per family, so this prefers the curated family
    when one matches and falls back to a model-level listing when the family
    has not been curated yet, so no evidence is lost. Shared by certification
    and the reviewer's create-listings action so the two cannot disagree
    about granularity and produce both entries for one part.
    """
    from lumina.results.component_match import (
        family_for_model,
        find_or_create_component,
    )

    family = family_for_model(raw_model, kind, vendor=vendor)
    if family is not None:
        return family, False
    return find_or_create_component(vendor, raw_model, kind, **kw)


def record_identity_alias(run: TestRun, listing, *, by=None) -> object | None:
    """Remember that this run's reported identity means ``listing``.

    Called when a listing is created or linked off the back of a run, so the
    manual work of interpreting firmware strings is done once. Without it, a
    machine whose DMI says vendor "OEM" product "7D2XCTO1WW" matches nothing on
    every subsequent run and each submitter has to work out for themselves that
    it is a ThinkSystem SR645 - and may name it differently, forking the
    catalog.

    Skipped when the reported strings already match the listing by name, since
    ``find_matching_system`` finds those unaided and an alias would just be
    noise. Also skipped when the reported product is blank: there is nothing to
    key on, and a blank-to-listing mapping would claim every unidentifiable
    machine is this one.
    """
    # Never from a scoped run. This table is keyed on the reported *machine* strings and is
    # global and singular, so one entry teaches every future run of that instance type to attach
    # itself to whatever this one produced. The strings a cloud guest reports identify a rental
    # SKU, not hardware, and a scoped run never claimed the machine in the first place.
    if run.is_scoped:
        return None

    identities = reported_identities(run)
    if not identities:
        return None
    # The most specific string the run actually carries: its system model when
    # there is one, otherwise its board. Keyed on what will recur, not on what
    # the submitter decided the machine is.
    reported_vendor, reported_product = identities[0]
    reported_vendor = (reported_vendor or "").strip()
    reported_product = (reported_product or "").strip()
    # Only a kind somebody actually stated. Deriving it from the listing type
    # would be wrong: a System entry can be created for a custom build - a board
    # cataloged as a system - so linking one proves nothing about the machine.
    # With nobody having said, this stays blank rather than guessing.
    declared = (run.listing_proposal or {}).get("machine_kind")
    resolved_kind = (
        declared if declared in (SystemKind.PREBUILT, SystemKind.CUSTOM) else ""
    )

    # Skip only when the mapping would carry nothing the run does not already
    # convey: the ordinary matchers find the listing unaided *and* there is no
    # kind correction to preserve. Prebuilt systems routinely fail to identify
    # themselves - a vendor mirroring its system name into the baseboard reads
    # as a custom build - and that correction is worth keeping even when the
    # listing itself matches by name.
    found_unaided = (
        find_matching_system(reported_vendor, reported_product) == listing
        if isinstance(listing, System)
        else find_matching_board(reported_vendor, reported_product) == listing
    )
    corrects_kind = bool(resolved_kind) and resolved_kind != run.system_kind
    if found_unaided and not corrects_kind:
        return None

    fk_kw = (
        listing_fk(listing)
    )
    alias, created = ReportedIdentityAlias.objects.get_or_create(
        reported_vendor=reported_vendor,
        reported_product=reported_product,
        defaults={"source_run": run, "created_by": by,
                  "resolved_kind": resolved_kind, **fk_kw},
    )
    if created:
        log_action(
            "reported_identity.alias",
            target=listing,
            actor=by,
            after={"reported_vendor": reported_vendor,
                   "reported_product": reported_product,
                   "resolved_kind": resolved_kind,
                   "listing": str(listing)},
        )
    return alias


def apply_vendor_maintained_fields(run: TestRun, listing) -> None:
    """Copy the two listing-maintenance fields onto an **existing** listing.

    Only for a submitter who speaks for that listing's vendor, and only for fields they
    actually filled in - a blank means "no change", not "erase what is there".

    This is a write to an existing listing from a run, which is the thing the
    listing-details gate exists to prevent, so the narrowness is the point: the form does
    not even show these fields to anyone else (``_drop_maintenance_fields``), this checks
    the same permission again server-side, and a reviewer approves the run before any of
    it lands. The identity fields - vendor, name, model number - are still never applied
    to an existing listing by anybody.
    """
    if not represents_listing_vendor(run.submitter, listing):
        return
    proposal = run.listing_proposal or {}
    changed = []
    for field in ("description", "vendor_spec_url"):
        value = (proposal.get(field) or "").strip()
        if value and value != getattr(listing, field):
            setattr(listing, field, value)
            changed.append(field)
    if changed:
        listing.save(update_fields=changed)


def apply_proposal_metadata(run: TestRun, listing) -> None:
    """Bind the submitter's taxonomy and release answers to a new listing.

    Category values proposed as new text are not bound: they become pending
    CategoryValue rows for an admin to approve, so a submitter cannot mint a
    filter option by typing one. Releases the submitter ticked are recorded as
    declared support; the release the run itself passed on is recorded
    separately by ``record_compatibility``, which is the evidence.
    """
    from lumina.hardware.models import ListingCategoryValue, ListingVersion
    from lumina.releases.models import AlmaLinuxRelease
    from lumina.taxonomy.models import Category, CategoryValue

    proposal = run.listing_proposal or {}
    fk_kw = (
        listing_fk(listing)
    )

    for key, raw in proposal.items():
        if not key.startswith(proposal_keys.CATEGORY_PREFIX) or not raw:
            continue
        slug = key[len(proposal_keys.CATEGORY_PREFIX):]
        category = Category.objects.filter(slug=slug).first()
        if category is None:
            continue
        chosen = [raw] if isinstance(raw, str) else list(raw)
        values = CategoryValue.objects.filter(
            category=category, slug__in=chosen, status=CategoryValue.STATUS_APPROVED
        )
        for value in values:
            ListingCategoryValue.objects.get_or_create(value=value, **fk_kw)

    for key, raw in proposal.items():
        if not key.startswith(proposal_keys.PROPOSE_PREFIX) or not str(raw).strip():
            continue
        slug = key[len(proposal_keys.PROPOSE_PREFIX):]
        category = Category.objects.filter(slug=slug).first()
        if category is None or not category.allow_suggestions:
            continue
        text = str(raw).strip()
        if CategoryValue.objects.filter(category=category, value__iexact=text).exists():
            continue
        CategoryValue.propose(
            category=category, value=text, proposed_by=run.submitter
        )

    for key, raw in proposal.items():
        if not proposal_keys.is_release_key(key):
            continue
        if not raw:
            continue
        try:
            major = int(key[len(proposal_keys.RELEASE_PREFIX):])
        except ValueError:
            continue
        release = AlmaLinuxRelease.objects.filter(major=major).first()
        if release is None:
            continue
        ListingVersion.objects.get_or_create(
            release=release,
            defaults={"source": ListingVersion.SOURCE_DECLARED},
            **fk_kw,
        )


def submitted_cpu_model(run: TestRun) -> str:
    """The CPU model to catalog: the submitter's correction, else the run's.

    The collector reports the specific part and that is what gets logged, since
    benchmarks are per model and the family is derived from it. The submitter
    can correct the string because DMI and lscpu both report things vendors did
    not intend as product names.
    """
    proposed = ((run.listing_proposal or {}).get("cpu_model") or "").strip()
    return proposed or (run.cpu_model or "").strip()


def submitted_cpu_family(run: TestRun):
    """The family component the submitter picked when no model was detected."""
    from lumina.hardware.models import Component, ComponentKind, ComponentRole

    raw = (run.listing_proposal or {}).get("cpu_family")
    if not raw:
        return None
    return Component.objects.filter(
        pk=raw, kind=ComponentKind.cpu.value, role=ComponentRole.FAMILY
    ).first()


def tieable_nics(run: TestRun) -> list[dict]:
    """NICs a run is evidence for: the ones that are named and have a bound driver.

    Reported as NICs never appearing among a run's components. Two reasons, and both had to go:
    the collector recorded no vendor or model for them - "enp2s0 running r8169" names no product -
    and this function did not exist, so ``component_tie_targets`` emitted a board, a CPU, and GPUs
    and nothing else.

    The driver requirement is the same rule ``tieable_gpus`` applies, and it matters more here. A
    network port with no driver is a PCI device that answered on the bus; nothing configured it,
    no test moved a packet through it, and cataloguing it would attest hardware that did not work.
    That is precisely the case a certification catalog must not get wrong.

    Every port of a card is reported separately and they collapse in ``component_tie_targets``,
    which drops targets whose ``tie_key`` it has already emitted - a four-port card is one
    component, and four rows for one part would read as four separate pieces of evidence.

    Naming is ``nic_identity``, which reads whatever the bundle carries: the full set of lspci
    names for a recent one, the flattened pair for an older one. A NIC that cannot be named ties
    nothing rather than tying something unnamed - which is what a report with no lspci output at
    all leaves, and what a USB adapter leaves.

    Returns copies carrying the resolved ``vendor`` and ``model``, so callers do not each repeat
    the choosing.
    """
    tieable = []
    for nic in (run.inventory.get("summary") or {}).get("nics") or []:
        vendor, model = nic_identity(nic)
        if vendor and model and nic.get("driver"):
            tieable.append({**nic, "vendor": vendor, "model": model})
    return tieable


def tieable_gpus(run: TestRun) -> list[dict]:
    """GPUs a run is actually evidence for: the ones with a bound driver.

    A GPU with no driver was never validated. All the run established is that a
    PCI device answers on the bus - the kernel never initialized it, nothing
    used it, and no test exercised it. Cataloging that as a component and
    attaching it to the system as certified would be an attestation about
    hardware nobody tested, which is how a BMC display adapter with no driver
    ended up as a published component collecting attestations.

    ``validate.gpu.driver`` still *reports* driverless GPUs, so they are visible
    in the run; they simply do not become catalog entries.
    """
    from lumina.results.component_match import (
        GPU_MARKETING_RE,
        integrated_gpu_name,
    )

    resolved = []
    for gpu in (run.inventory.get("summary") or {}).get("gpus") or []:
        vendor, model = gpu_identity(gpu)
        if vendor and model and gpu.get("driver"):
            resolved.append({**gpu, "vendor": vendor, "model": model})

    # Name an AMD integrated GPU from the CPU it lives on.
    #
    # pci.ids has no marketing name for those dies, so lspci reports a codename ("Phoenix1") and
    # the part is unsearchable; the APU's own brand string carries the name. This lived in the
    # collector, which rewrote the model before the report was written - and it keyed on a
    # flattened vendor token that no longer exists, so it silently stopped working the moment the
    # collector started reporting lspci's strings instead. Nothing caught that: only the pure
    # string helper was covered.
    #
    # Only when exactly one AMD GPU lacks a marketing name. On a machine with both an APU and a
    # discrete AMD card the discrete one is bracketed by pci.ids and keeps its own name; if
    # neither were identifiable there would be no way to tell which the CPU string describes.
    name = integrated_gpu_name(run.cpu_model or "")
    if name:
        unnamed = [
            gpu for gpu in resolved
            if "amd" in gpu["vendor"].lower()
            and not GPU_MARKETING_RE.search(gpu["model"])
        ]
        if len(unnamed) == 1:
            # The codename is kept: it identifies the silicon generation in a way the marketing
            # name does not.
            unnamed[0]["asic"] = unnamed[0]["model"]
            unnamed[0]["model"] = name
    return resolved


def tie_key(kind, raw_model: str) -> str:
    """A stable name for one component tie, used to exclude it.

    Kind plus the reported model, case-folded and whitespace-collapsed. Not the resolved
    component's pk: the whole point is to refuse a tie *before* anything is created, so at
    the moment of excluding it there may be no row to point at.

    Keyed on what the report said rather than on position in the list, so re-uploading the
    same machine keeps an exclusion pinned to the part it was about even if the GPU order
    changes between runs.
    """
    kind_value = getattr(kind, "value", kind)
    model = " ".join((raw_model or "").split()).casefold()
    return f"{kind_value}:{model}"


def component_tie_targets(run: TestRun) -> list[dict]:
    """The parts a run is evidence for, as (key, brand, model, kind) entries.

    The single source for both ``ensure_component_ties``, which resolves and links them, and
    ``preview_component_ties``, which only reports them - so the reviewer's preview cannot
    drift from what approving actually does.

    Each entry carries a ``key`` from ``tie_key``, and that key is computed from what the
    *report* said, before any override is applied. It has to be: the key is what an exclusion
    and an override are both filed under, so deriving it from a corrected model would move
    the very row somebody was correcting, unpinning their earlier decision the moment they
    made this one.

    ``brand`` and ``raw_model`` are what the tie will actually use, which is the reported
    value unless somebody has corrected it. DMI and lspci are frequently wrong or unhelpful -
    "OEM", "0M83RH", "CometLake-S GT2 [UHD Graphics 630]" - and only the person holding the
    machine, or a reviewer, can say what the part really is. Left uncorrected, a bad vendor
    string mints a catalog manufacturer named after it.
    """
    overrides = run.component_overrides or {}

    def entry(kind, brand: str, raw_model: str, attributes: dict) -> dict:
        key = tie_key(kind, raw_model)
        chosen = overrides.get(key) or {}
        return {
            "key": key,
            "kind": kind,
            # What the report said, kept so the form can show the correction against it.
            "reported_brand": brand,
            "reported_model": raw_model,
            "brand": (chosen.get("brand") or "").strip() or brand,
            "raw_model": (chosen.get("model") or "").strip() or raw_model,
            "overridden": bool(
                (chosen.get("brand") or "").strip()
                or (chosen.get("model") or "").strip()
            ),
            "attributes": attributes,
        }

    targets: list[dict] = []
    if run.board_vendor and run.board_model:
        targets.append(entry(
            ComponentKind.motherboard, run.board_vendor, run.board_model, {},
        ))

    # The *reported* model, like every other kind, because ``tie_key`` is what every stored
    # answer about this part is filed under.
    #
    # This used ``submitted_cpu_model``, which prefers the submitter's correction - so correcting
    # the CPU model moved the key, and every answer already filed against the old one stopped
    # matching: a recorded decline, an exclusion, a brand fix. Harmless while an unanswered claim
    # meant "no claim"; now that the ticked box is honoured, an orphaned decline is silently
    # refilled with a vendor claim the reader had explicitly turned down.
    #
    # The correction is not lost: ``entry()`` applies ``component_overrides[key]["model"]`` over
    # the reported string, so the row still shows and ties what the submitter typed.
    if run.cpu_model:
        targets.append(entry(
            ComponentKind.cpu,
            CPU_VENDOR_NAMES.get(
                (run.cpu_vendor or "").lower(), run.cpu_vendor or "Unknown"
            ),
            run.cpu_model,
            {},
        ))

    for gpu in tieable_gpus(run):
        # Already resolved by ``gpu_identity``; no second translation here, which is where the
        # token-to-display-name mapping used to happen.
        targets.append(entry(
            ComponentKind.gpu,
            gpu["vendor"],
            gpu["model"],
            {
                key: value
                for key, value in (
                    ("driver", gpu.get("driver")),
                    ("driver_version", gpu.get("driver_version")),
                )
                if value
            },
        ))

    for nic in tieable_nics(run):
        # No vendor-name map, unlike CPUs and GPUs. Those have three vendors between them; the
        # NIC space is Realtek, Broadcom, Mellanox, Marvell, Aquantia, and a long tail, and a
        # hardcoded table would be permanently one entry short. ``resolve_vendor`` and the alias
        # table already do this job for every other free-text vendor on the site.
        targets.append(entry(
            ComponentKind.nic,
            nic["vendor"],
            nic["model"],
            {
                key: value
                for key, value in (
                    ("driver", nic.get("driver")),
                    ("driver_version", nic.get("driver_version")),
                    ("firmware", nic.get("firmware")),
                )
                if value
            },
        ))

    # One entry per part, not per device.
    #
    # A dual-port card reports two interfaces and a machine can hold two identical GPUs; both are
    # one catalog component. Without this the form showed a row per device, two checkboxes sharing
    # one ``included_ties`` value, and a summary claiming two pieces of evidence for one part.
    #
    # ``tie_key`` normalizes, so "BCM57414 ... Ethernet Controller" from both ports collapses.
    # Deduplicated here rather than per kind because the same is true of every kind - a latent
    # version of this existed for duplicate GPUs long before NICs were emitted at all.
    seen: set[str] = set()
    unique = []
    for target in targets:
        # A scoped run ties only what it claims. Here rather than in each consumer, because this
        # function is the single source for the submitter's checkboxes, the reviewer's preview, and
        # the ties actually made, so all three follow with no second copy of the rule.
        #
        # Nothing is hidden by this. The full inventory is still on the run page and in the bundle,
        # and that is where the CPU and the NIC of a GPU-scoped cloud run belong: they are context
        # for the reader, not things this run proved anything about.
        if run.is_scoped and getattr(target["kind"], "value", target["kind"]) not in run.claim_scope:
            continue
        if target["key"] in seen:
            continue
        seen.add(target["key"])
        unique.append(target)
    return unique


def preview_component_ties(run: TestRun) -> list[dict]:
    """What approving this run would tie, resolved but creating nothing.

    The ties themselves are evidence, so they are only made on approval - which
    left the reviewer's component list empty right up to the moment of
    approving, with no way to see that the CPU and motherboard were about to be
    attached. This is the same resolution ``silicon_component`` does (curated
    family first, then an existing model) reported rather than applied, so
    entries say either which catalog listing they will attach to or that one
    will be created.
    """
    from lumina.results.component_match import (
        catalog_name,
        family_for_model,
        match_component,
    )

    excluded = set(run.excluded_component_ties or [])
    preview: list[dict] = []
    for target in component_tie_targets(run):
        vendor = resolve_vendor(target["brand"])
        family = None
        model = None
        if vendor is not None:
            if target["kind"] in (ComponentKind.cpu, ComponentKind.gpu):
                # Only CPUs and GPUs have curated families; a motherboard has
                # nothing to roll up to.
                family = family_for_model(
                    target["raw_model"], target["kind"], vendor=vendor
                )
            model = match_component(vendor, target["raw_model"], target["kind"])
        key = target["key"]
        preview.append({
            "key": key,
            # Reported rather than filtered out, so an excluded part stays visible with
            # its state showing. Dropping it from the list would leave the submitter
            # unable to change their mind and the reviewer unable to see the decision.
            "excluded": key in excluded,
            "kind": target["kind"].value,
            "kind_label": target["kind"].label,
            "brand": vendor.name if vendor else target["brand"],
            "raw_model": target["raw_model"],
            # What the report said, forwarded so a form can show the correction against it
            # and a reader can see that one was made.
            "reported_brand": target["reported_brand"],
            "reported_model": target["reported_model"],
            "overridden": target["overridden"],
            # What this string translates to, whether or not anything matches it yet. For a
            # part the catalog already knows, ``component`` says where it lands; for a new
            # one this is the only place the name it would be created under appears at all.
            "catalog_name": catalog_name(vendor, target["raw_model"], target["kind"]),
            # Whether approving adds a catalog entry that does not exist yet,
            # stated outright rather than left to be inferred from an absence.
            "will_create": (family or model) is None,
            # And whether it also mints a *manufacturer*. _vendor_for creates
            # one when nothing resolves, which is how a vendor called "OEM"
            # would have appeared - a reviewer has to be able to catch that
            # before it is in the catalog, not after.
            "new_vendor": vendor is None,
            # The specific part, and the group it rolls up to. Both are shown
            # because they mean different things: certification applies to the
            # family, benchmark results are recorded against the model, and a
            # reviewer seeing only "AMD EPYC 7003 Series" cannot tell which
            # processor produced the evidence.
            "model": model,
            "family": family,
            # What actually gets attached: the family when one is curated, as
            # silicon_component decides.
            "component": family or model,
        })

    # The manual path: no CPU model was detected, so the submitter named a
    # family outright. It exists already by definition, and there is no
    # specific part to show alongside it.
    if not any(entry["kind"] == ComponentKind.cpu.value for entry in preview):
        family = submitted_cpu_family(run)
        if family is not None:
            key = tie_key(ComponentKind.cpu, family.name)
            preview.append({
                "key": key,
                "excluded": key in excluded,
                "kind": ComponentKind.cpu.value,
                "kind_label": ComponentKind.cpu.label,
                "brand": family.vendor.name,
                "raw_model": None,
                # No report to differ from: the submitter named this family outright.
                "reported_brand": family.vendor.name,
                "reported_model": None,
                "overridden": False,
                "will_create": False,
                "new_vendor": False,
                "model": None,
                "family": family,
                "component": family,
            })
    return preview


# Board, then processor, then everything else - the order in which a machine is identified.
_KIND_ORDER = {
    ComponentKind.motherboard.value: 0,
    ComponentKind.cpu.value: 1,
    ComponentKind.gpu.value: 2,
    ComponentKind.nic.value: 3,
}


def group_component_rows(entries) -> list[dict]:
    """Group tie entries by component kind.

    Grouped because a machine reports several parts of a few kinds, and a flat list makes a
    reviewer scan for "which of these is the CPU".

    Takes the entries rather than the run so the reviewer's form can group *its own* rows - the
    ones carrying the form fields - through the same ordering. It used to be two renderings of
    the same data on one page: a grouped read-only list headed "Will be attached on approval"
    and a flat editable one headed "Adjust before approving", which is the shape this module's
    own docstrings warn about.
    """
    groups: dict[str, dict] = {}
    for entry in entries:
        group = groups.setdefault(
            entry["kind"],
            {"kind": entry["kind"], "label": entry["kind_label"], "entries": []},
        )
        group["entries"].append(entry)
    return sorted(
        groups.values(), key=lambda g: (_KIND_ORDER.get(g["kind"], 99), g["label"])
    )


def preview_component_groups(run: TestRun) -> list[dict]:
    """``preview_component_ties`` grouped by kind."""
    return group_component_rows(preview_component_ties(run))


def ensure_component_ties(run: TestRun) -> None:
    """Tie a passing validation run's motherboard, CPU, and GPUs to the catalog.

    Every certified machine implicitly certifies the parts it runs on, so the board, CPU, and
    each GPU are matched to existing components (exact model, alias, or a reviewer-curated
    family pattern like "EPYC 9004 Series" / "GeForce RTX 40 Series") or created fresh from
    the cleaned model string. The components join the run's linked components, so they
    collect the attestation with everything else; when the run is linked to a System, the
    CPU attaches to that system's certified-CPU set and GPUs to its related components.

    Driven by ``component_tie_targets``, which is what that function's docstring always
    claimed ("one source so the reviewer's preview cannot drift from what approving actually
    does") and was not: this function re-derived the same board/CPU/GPU triples itself. The
    two stayed in step only as long as nobody changed one of them, and every feature since
    has had to be written twice - exclusions were, and the per-component vendor and model
    overrides would have been. A preview that lies about what approval does is worse than no
    preview.

    What differs per kind, and why the loop is not uniform:

    - a **motherboard** has no curated families to roll up to, so it resolves with
      ``find_or_create_component`` rather than ``silicon_component``, and attaches to the
      system as a related component;
    - a **CPU** rolls up to its family and attaches to the system's certified-CPU set;
    - a **GPU** rolls up to its family and carries driver attributes.
    """
    from lumina.hardware.services import attach_cpu
    from lumina.results.component_match import find_or_create_component

    # Not ``certifies``: this records which parts the run touched, which is worth having even when
    # the run moves nobody's standing. ``scoped_listings`` and ``_apply_attestation`` decide what
    # any of it certifies.
    if run.run_type != RunType.validate.value or run.verdict() is not True:
        return

    excluded = set(run.excluded_component_ties or [])
    tied = []

    for target in component_tie_targets(run):
        if target["key"] in excluded:
            continue
        kind = target["kind"]
        vendor = _vendor_for(target["brand"])
        if kind == ComponentKind.motherboard:
            component, created = find_or_create_component(
                vendor, target["raw_model"], kind, created_by=run.submitter,
            )
        else:
            component, created = silicon_component(
                vendor, target["raw_model"], kind, created_by=run.submitter,
                extra_attributes=target["attributes"] or None,
            )
        if component is None:
            continue
        run.listing_components.add(component)
        if run.listing_system_id:
            if kind == ComponentKind.cpu:
                attach_cpu(run.listing_system, component)
            else:
                # A board is one part of a larger prebuilt model; on a custom build there
                # is no system and the board itself is the identity.
                run.listing_system.related_components.add(component)
        tied.append({"component": component.pk, "kind": kind.value,
                     "created": created, "raw_model": target["raw_model"]})

    # The manual path, which is not a *target* because there is no model to resolve: no CPU
    # string was reported anywhere, so the submitter named a family outright. Certification
    # is at family level regardless, so a family alone is complete evidence even though no
    # benchmark result can ever be attributed to it.
    if not any(entry["kind"] == ComponentKind.cpu.value for entry in tied):
        family = submitted_cpu_family(run)
        if family is not None:
            run.listing_components.add(family)
            if run.listing_system_id:
                attach_cpu(run.listing_system, family)
            tied.append({"component": family.pk, "kind": ComponentKind.cpu.value,
                         "created": False, "raw_model": None})

    if tied:
        log_action("test_run.component_ties", target=run, after={"tied": tied})


def _loosens_gate(incoming: int | None, current: int | None) -> bool:
    """Whether ``incoming`` is a weaker gate than ``current``.

    None is the weakest of all: no minor to wait for. Otherwise a lower minor is weaker, since
    it ships sooner. Called with the run's answer and the row's, and only a weakening is written.
    """
    if current is None:
        return False
    if incoming is None:
        return True
    return int(incoming) < int(current)


def record_compatibility(run: TestRun) -> list:
    """Record AlmaLinux compatibility on everything a passing run is tied to.

    A run that passed on AlmaLinux 9.6 is direct evidence the system (and its tied components -
    CPU family, board, GPU) work on 9. One row per major, and that is the whole claim.

    It used to carry a ``minimum_minor`` floor as well, created at the run's minor and lowered
    by earlier evidence. That was evidence-honest and it is gone deliberately: hardware now
    certifies per major, the way the software catalog always has. The minor is still recorded on
    the run, which is where the provenance of the evidence belongs.
    """
    from lumina.hardware.models import ListingVersion

    if not certifies(run):
        return []
    if run.alma_release_id is None or run.alma_minor is None:
        return []

    # Same rule as ``_apply_attestation``, and for the same reason: a compatibility row saying
    # "this server works on AlmaLinux 9" is a certification claim in the catalog's own table, so a
    # scoped run must not write one for a machine it made no claim about.
    targets = scoped_listings(run)

    recorded = []
    for listing in targets:
        fk_kw = listing_fk(listing)
        # One row per (listing, release) is enforced by ListingVersion's uniques on
        # every backend now that their redundant conditions are gone. The lock is
        # still worth having: it makes two concurrent approvals of the same hardware
        # serialize rather than one of them dying on an IntegrityError that
        # get_or_create would surface to the reviewer as a 500.
        #
        # Own atomic block so the lock is valid whichever caller we are under; a
        # no-op savepoint when that caller is already atomic, as approve_run is.
        with transaction.atomic():
            type(listing).objects.select_for_update().filter(pk=listing.pk).first()
            version, created = ListingVersion.objects.get_or_create(
                release=run.alma_release,
                defaults={"source": ListingVersion.SOURCE_RUN,
                          "available_from_minor": run.available_from_minor},
                **fk_kw,
            )
            changed = []
            if version.source != ListingVersion.SOURCE_RUN:
                # A run has now proven what was previously only declared.
                version.source = ListingVersion.SOURCE_RUN
                changed.append("source")
            # A gate only ever loosens. Evidence from a shipped release supersedes a Kitten
            # claim outright - somebody has now proved the hardware works on something people
            # can install - and where two Kitten runs disagree the earlier minor is the better
            # news and the one already proved. Tightening here would let a later run put a
            # disclaimer back on a claim that had earned its way out of one.
            if _loosens_gate(run.available_from_minor, version.available_from_minor):
                version.available_from_minor = run.available_from_minor
                changed.append("available_from_minor")
            if changed:
                version.save(update_fields=changed)
        # The minor is still logged, on the run itself, and shown wherever the run is - it is
        # provenance for this evidence rather than the scope of the claim.
        recorded.append(
            {"listing": str(listing), "release": run.alma_release.major,
             "run_minor": run.alma_minor}
        )
    if recorded:
        log_action("test_run.compatibility", target=run, after={"recorded": recorded})
    return recorded


def record_architecture(run: TestRun) -> list:
    """Bind the architecture the run's kernel reported to everything it is tied to.

    The only hardware taxonomy facet left, and the only one worth keeping, because
    it is the only one nobody has to fill in: ``environment.os.arch`` is in every
    bundle. Network, Storage, and PCIe Generation were removed for failing that
    test - a facet set on some listings and blank on others makes an empty filter
    result read as "no such hardware".

    Runs next to ``record_compatibility`` and on the same trigger, because it is
    the same kind of statement: what an approved, passing run proves about the
    hardware it ran on.

    Only values the taxonomy already lists are bound. A kernel reporting something
    curated values do not cover is skipped rather than quietly adding a facet value
    nobody approved - the arch list is what the Foundation builds for, and
    extending it is an admin decision.

    Additive, never subtractive: the same catalog entry can legitimately hold two
    arches when a model shipped as both an x86_64 and an aarch64 machine.
    """
    from lumina.hardware.models import ListingCategoryValue
    from lumina.taxonomy.models import CategoryValue

    if not certifies(run):
        return []
    arch = ((run.environment or {}).get("os") or {}).get("arch")
    if not arch:
        return []

    value = (
        CategoryValue.objects.approved()
        .filter(
            category__derived_from_runs=True,
            category__slug="architecture",
            value__iexact=arch,
        )
        .first()
    )
    if value is None:
        log_action(
            "test_run.architecture_unrecognised", target=run,
            after={"reported": arch},
        )
        return []

    # ``scoped_listings`` for the third time. An architecture facet is a fact published on a
    # catalog page, so a scoped run must not write one onto a machine it made no claim about.
    recorded = []
    for listing in scoped_listings(run):
        fk = listing_fk(listing)
        _, created = ListingCategoryValue.objects.get_or_create(value=value, **fk)
        if created:
            recorded.append({"listing": str(listing), "architecture": value.value})
    if recorded:
        log_action("test_run.architecture", target=run, after={"recorded": recorded})
    return recorded


class ReviewError(Exception):
    """Raised when a review action is not valid for a run's current state."""


def _require_open(run: TestRun, action: str) -> None:
    if run.status == TestRun.STATUS_DRAFT:
        raise ReviewError(
            f"Cannot {action} a run the submitter has not finished; it is "
            "still awaiting their listing details."
        )
    if run.status == TestRun.STATUS_QUARANTINED:
        # Named rather than left to the generic message, which would say
        # status='quarantined' and leave the reviewer to work out what to do.
        raise ReviewError(
            f"Cannot {action} a run that was not performed on AlmaLinux "
            f"(it reports {run.host_os_id or 'no operating system'}). Release it "
            "from quarantine first, and only if the reported OS is wrong."
        )
    if run.status not in TestRun.OPEN_STATUSES:
        raise ReviewError(f"Cannot {action} a run with status={run.status!r}.")


def _require_supported_os(run: TestRun, action: str) -> None:
    """The OS gate, independent of ``status``.

    ``_require_open`` already rejects a quarantined run, so on the ordinary path
    this never fires. It exists for the path where it would: someone editing
    ``status`` directly in the admin, or a future code path that sets a status
    without going through ingest. Losing this check would mean a report saying
    "rocky" could be published as AlmaLinux certification.
    """
    if not run.may_certify_almalinux:
        raise ReviewError(
            f"Cannot {action} a run that reports {run.host_os_id!r} rather than "
            "AlmaLinux. Only AlmaLinux runs can certify an AlmaLinux release."
        )


def missing_submission_details(run: TestRun) -> list[str]:
    """What the submitter still owes before a validation run can be reviewed.

    Only asks for what the suite genuinely cannot derive, but it does ask for
    every kind of machine. Custom builds used to be exempted on the grounds
    that they "have no system listing to complete", which had it backwards: a
    custom build's motherboard *is* its listing, so brand-new hardware went to
    review with nothing a human had supplied and the catalog gained a
    motherboard entry named after whatever DMI happened to say.
    """
    if run.run_type != RunType.validate.value:
        return []
    if run.listing_system_id or run.listing_components.exists():
        return []

    # A scoped run owes nothing about the machine, because it is not a claim about the machine and
    # can never carry a System listing. Left unguarded, the branches below asked a submitter for the
    # host's catalog details and *disabled the submit button* until they were supplied, which is the
    # cloud case scoping exists for: a card in a rented instance whose SKU is not in the catalog and
    # must never be added to it. What it does owe is a subject, since ``create_listings_from_run``
    # has nothing to tie or certify when the claimed kind was never detected.
    if run.is_scoped:
        if run.claim_subject:
            return []
        kinds = " or ".join(run.scope_labels)
        return [
            f"the {kinds} this run is about: nothing in the inventory identifies one, "
            "so there is nothing to certify"
        ]

    if run.system_kind == SystemKind.PREBUILT:
        if find_matching_system(run.system_vendor, run.system_product):
            return []      # will auto-link on release
        if not run.listing_proposal:
            return ["listing details for a system that is not in the catalog yet"]
        return []

    # Custom, the fallback kind, covers two situations and they need different words.
    #
    # A self-build is identified by its board, so the ask names the board. A machine whose
    # firmware named no board manufacturer either has nothing to match against and nothing to
    # prefill, and the ask has to say so - otherwise it points at a motherboard the submitter
    # cannot see named anywhere.
    #
    # Split on the identity rather than on a third ``SystemKind``. That is what "unknown" was
    # standing in for here, and it was the wrong thing to encode as a kind: whether a board can
    # serve as an identity is a question about this run's data, not about what the machine is.
    if is_placeholder(run.board_vendor) or is_placeholder(run.board_model):
        if not run.listing_proposal:
            return [
                "listing details: this machine's firmware does not identify its "
                "manufacturer or model, so they have to be supplied by hand"
            ]
        return []

    if find_matching_board(run.board_vendor, run.board_model):
        return []
    if not run.listing_proposal:
        return [
            "listing details for the motherboard, which is what identifies "
            "a custom build"
        ]
    return []


def find_matching_board(vendor_name: str, model: str):
    """Existing motherboard component for a reported board, or None.

    Alias first, for the same reason as systems - and it matters more here,
    because a custom build is identified by its board, and unbranded firmware
    reports no board manufacturer at all.
    """
    from lumina.hardware.models import Component
    from lumina.results.component_match import match_component
    from lumina.vendors.services import resolve_vendor

    aliased = ReportedIdentityAlias.resolve(vendor_name, model)
    if isinstance(aliased, Component):
        return aliased
    if not (vendor_name or "").strip() or not (model or "").strip():
        return None
    vendor = resolve_vendor(vendor_name)
    if vendor is None:
        return None
    return match_component(vendor, model, ComponentKind.motherboard)


def submit_group_for_review(run: TestRun, *, by) -> tuple[list, list]:
    """Submit this run and the submitter's other drafts of the same machine.

    Returns ``(submitted, blocked)`` where blocked entries are
    ``(run, reason)``. Deliberately not atomic: submitting two of three runs is
    a better outcome than refusing all three because one still needs something,
    and the caller reports both halves rather than leaving the submitter to
    guess which went.

    Only sibling *drafts* come along. A run the reviewer sent back was bounced
    for a reason, and sweeping it into a bulk submit would resubmit it without
    anyone having addressed that reason.
    """
    group = [run, *sibling_draft_runs(run)]
    submitted, blocked = [], []
    for member in group:
        try:
            submit_for_review(member, by=by)
        except ReviewError as exc:
            blocked.append((member, str(exc)))
        else:
            submitted.append(member)
    return submitted, blocked


@transaction.atomic
def submit_for_review(run: TestRun, *, by) -> TestRun:
    """Submitter releases their completed draft into the review queue.

    Accepts a run the reviewer sent back as well as a fresh draft. Restricting
    this to drafts made "needs changes" a dead end: the reviewer asked for
    something, and the submitter had no way to act on it - no edit route, no
    resubmit, and no sight of what was asked.
    """
    if run.status not in SUBMITTABLE_STATUSES:
        raise ReviewError(
            f"A run with status {run.get_status_display()!r} cannot be "
            "submitted for review."
        )
    outstanding = missing_submission_details(run)
    if outstanding:
        raise ReviewError(
            "Still needed before review: " + "; ".join(outstanding) + "."
        )
    # A late-arriving catalog entry means this can link now even though it
    # could not at ingest.
    auto_link_existing_system(run)
    run.status = TestRun.STATUS_PENDING
    run.save(update_fields=["status"])
    log_action("test_run.submit_for_review", target=run, actor=by,
               after={"status": run.status})
    return run


@transaction.atomic
def approve_run(run: TestRun, *, by, notes: str = "") -> TestRun:
    """Approve a run, publishing it unless it is embargoed.

    An embargoed run (pre-release with a future requested publish date) is
    approved but left unpublished; ``publish_due_runs`` releases it on the
    day. A validate run linked to a listing also contributes to that
    listing's certification standing - see ``_apply_attestation``.
    """
    _require_open(run, "approve")
    _require_supported_os(run, "approve")
    run.status = TestRun.STATUS_APPROVED
    run.reviewed_by = by
    run.reviewed_at = timezone.now()
    if notes:
        run.reviewer_notes = notes

    today = timezone.localdate()
    # A hold with no date is a hold until somebody lifts it.
    #
    # This required a *future* date, so ticking "unreleased hardware" and leaving the date blank
    # published everything immediately - the opposite of what the tick says, and the opposite of
    # what the form's own help text promised. A submitter who does not yet know the announcement
    # date is the ordinary case for unreleased hardware, and it is exactly when they most need
    # the hold.
    #
    # A date in the past still publishes at once: it has already arrived.
    embargoed = run.pre_release and (
        run.publish_requested_date is None or run.publish_requested_date > today
    )
    run.published_at = None if embargoed else timezone.now()
    run.save(
        update_fields=[
            "status", "reviewed_by", "reviewed_at", "reviewer_notes", "published_at",
        ]
    )

    # Approving a validation run is what puts its hardware in the catalog, so
    # the listing is made here rather than behind a button the reviewer had to
    # remember to press first. There is no version of "approved" that should
    # leave a run with no listing: it would attest nothing, appear nowhere, and
    # look approved anyway. An existing listing - auto-linked at ingest or
    # assigned by the reviewer - is reused, never duplicated.
    #
    # If the run carries no usable identity at all, create_listings_from_run
    # raises and the approval fails with a message saying so. That is the
    # honest outcome: the reviewer has to say what this hardware is before
    # certifying it.
    listings_created = []
    if run.run_type == RunType.validate.value and not (
        run.listing_system_id or run.listing_components.exists()
    ):
        listings_created = [
            listing.pk for listing in create_listings_from_run(run, by=by)
        ]
    elif run.run_type == RunType.validate.value:
        # Already linked, so no listing is created and the block above is skipped
        # entirely - which is why the vendor's description had to be applied here as well
        # as inside create_listings_from_run. A run auto-linked at ingest never reaches
        # that function at all.
        #
        # Only the run's *primary* listing: the system, or for a custom build its
        # motherboard. Copying a machine's description onto every tied component would
        # put "2U dual-socket rack server" on a CPU family.
        primary = existing_listing_for(run)
        if primary is not None:
            apply_vendor_maintained_fields(run, primary)

    # Certification is withheld while the run is embargoed. Attesting would
    # publish the listing (see _attest_one), which would put unreleased
    # hardware in the catalog on the strength of a run nobody is allowed to
    # see - the exact placeholder the embargo exists to prevent. It is applied
    # by publish_due_runs on the release date instead.
    if run.is_embargoed:
        attested = False
    else:
        ensure_component_ties(run)
        # record_compatibility FIRST: an attestation now hangs off the release row
        # it is about, so the row has to exist. Reversed, a listing's first-ever
        # run would attest nothing at all.
        record_compatibility(run)
        record_architecture(run)
        attested = _apply_attestation(run)
    log_action(
        "test_run.approve",
        target=run,
        actor=by,
        after={
            "status": run.status,
            "published_at": run.published_at.isoformat() if run.published_at else None,
            # A dateless hold is embargoed with nothing to format, which this used to assume
            # away - the entry crashed on ``None.isoformat()`` the moment "no date" started
            # meaning "held".
            "embargoed_until": (
                run.publish_requested_date.isoformat()
                if embargoed and run.publish_requested_date is not None else None
            ),
            "attestation_created": attested,
            "listings_created": listings_created,
        },
        notes=notes,
    )
    return run


@transaction.atomic
def release_from_quarantine(run: TestRun, *, by, reason: str) -> TestRun:
    """Let a quarantined run into the normal review flow.

    This is not "approve anyway". It is the reviewer stating that the *report* is
    wrong about the operating system, which does happen: a rebuilt or minimised
    image can lose ``/etc/os-release``, and a container can inherit its base
    image's. The run then rejoins the queue at its ordinary starting point and
    still has to be reviewed on its merits.

    A reason is required, not optional. This is the one route by which a report
    saying "rocky" becomes AlmaLinux certification evidence, so the record has to
    say who decided that and on what grounds.

    The release also binds the AlmaLinux release, which ingest refused to do.
    ``parse_version`` rather than ``parse_release``: the gate the latter applies
    is exactly the one being overridden here.
    """
    from lumina.releases.models import AlmaLinuxRelease
    from lumina.results.ingest import normal_initial_status

    if run.status != TestRun.STATUS_QUARANTINED:
        raise ReviewError(
            f"Only a quarantined run can be released; this one is "
            f"{run.status!r}."
        )
    if not (reason or "").strip():
        raise ReviewError(
            "Say why the reported operating system is wrong. This is the only "
            "way a non-AlmaLinux report becomes AlmaLinux evidence, so the "
            "reason is part of the record."
        )

    was = run.host_os_id
    run.os_quarantine_released = True
    run.status = normal_initial_status(run.run_type)
    run.reviewer_notes = reason
    run.reviewed_by = by
    run.reviewed_at = timezone.now()

    major, minor = inventory_extract.parse_version(run.environment or {})
    if major is not None:
        run.alma_release = AlmaLinuxRelease.objects.filter(major=major).first()
        run.alma_minor = minor

    run.save(update_fields=[
        "os_quarantine_released", "status", "reviewer_notes", "reviewed_by",
        "reviewed_at", "alma_release", "alma_minor",
    ])
    log_action(
        "test_run.quarantine_release", target=run, actor=by,
        before={"status": TestRun.STATUS_QUARANTINED, "host_os_id": was},
        after={"status": run.status,
               "alma_release": run.alma_release.major if run.alma_release else None},
        notes=reason,
    )
    return run


def reject_run(run: TestRun, *, by, reason: str = "") -> TestRun:
    # Quarantined runs are rejectable: disposing of one is the ordinary outcome,
    # and without this a reviewer could only ever release it or leave it sitting
    # there.
    if run.status == TestRun.STATUS_QUARANTINED:
        run.status = TestRun.STATUS_REJECTED
        run.reviewer_notes = reason
        run.reviewed_by = by
        run.reviewed_at = timezone.now()
        run.save(update_fields=[
            "status", "reviewer_notes", "reviewed_by", "reviewed_at",
        ])
        log_action("test_run.reject", target=run, actor=by,
                   before={"status": TestRun.STATUS_QUARANTINED},
                   after={"status": run.status}, notes=reason)
        return run
    _require_open(run, "reject")
    run.status = TestRun.STATUS_REJECTED
    run.reviewer_notes = reason
    run.reviewed_by = by
    run.reviewed_at = timezone.now()
    run.save(update_fields=["status", "reviewer_notes", "reviewed_by", "reviewed_at"])
    log_action("test_run.reject", target=run, actor=by,
               after={"status": run.status}, notes=reason)
    return run


def request_run_changes(run: TestRun, *, by, reason: str = "") -> TestRun:
    if run.status != TestRun.STATUS_PENDING:
        raise ReviewError(
            f"Cannot request changes on a run with status={run.status!r}."
        )
    run.status = TestRun.STATUS_NEEDS_CHANGES
    run.reviewer_notes = reason
    run.reviewed_by = by
    run.reviewed_at = timezone.now()
    run.save(update_fields=["status", "reviewer_notes", "reviewed_by", "reviewed_at"])
    log_action("test_run.request_changes", target=run, actor=by,
               after={"status": run.status}, notes=reason)
    return run


def assign_listing(
    run: TestRun,
    *,
    system: System | None,
    by,
    level: str = "",
    components=None,
    machine_kind: str = "",
    available_from_minor: int | None = None,
    set_available_from_minor: bool = False,
    pre_release: bool = False,
    publish_requested_date=None,
    set_embargo: bool = False,
) -> TestRun:
    """Link a run to catalog listings (or unlink).

    Prebuilt machines link to a System listing. Custom builds have no vendor
    system model, so their runs link to the Components they exercised
    (motherboard, CPU); ``components`` replaces the current set when given.

    Normally done before approval, but linking an already-approved run
    applies the certification coupling immediately - otherwise the listing
    would show the run as evidence without its standing ever moving.

    A scoped run cannot be given a System. Refused rather than ignored, because a reviewer who
    picked one made a decision and is owed an answer about it. The components half stays open: a
    reviewer attaching the right card by hand is exactly what a scoped run needs when lspci
    resolved the card to nothing.
    """
    if system is not None and run.is_scoped:
        raise ReviewError(
            "This run is evidence for its "
            f"{' and '.join(run.scope_labels)} only, so it cannot be attached to a system "
            "listing. Attach the component instead."
        )
    run.listing_system = system
    if level:
        run.claimed_validation_level = level
    fields = ["listing_system", "claimed_validation_level"]
    # An explicit flag rather than "is it None", because None is a meaningful answer here:
    # clearing a gate a submitter should not have set is exactly what a reviewer needs to be
    # able to do, and it is indistinguishable from "did not say" without this.
    if set_available_from_minor:
        run.available_from_minor = available_from_minor
        fields.append("available_from_minor")
    if set_embargo:
        run.pre_release = bool(pre_release)
        run.publish_requested_date = publish_requested_date
        fields += ["pre_release", "publish_requested_date"]
    if machine_kind in (SystemKind.PREBUILT, SystemKind.CUSTOM):
        # Recorded on the run so the alias can carry it: a machine whose
        # firmware misidentifies it needs the correction to outlive this run.
        proposal = dict(run.listing_proposal or {})
        proposal["machine_kind"] = machine_kind
        run.listing_proposal = proposal
        fields.append("listing_proposal")
    run.save(update_fields=fields)
    # Clearing the embargo on a run that is approved but held is how a hold with no date ends:
    # nothing is scheduled for it, because there is no date to schedule against. Done here so
    # the reviewer's one edit both records the decision and carries it out.
    released = False
    if set_embargo and not run.is_embargoed_by_request and run.status == TestRun.STATUS_APPROVED:
        released = release_held_run(run)
    if components is not None:
        run.listing_components.set(components)
    # A reviewer linking a run by hand is the strongest statement available
    # about what its firmware strings mean, and it was the one path that
    # recorded nothing - so the next run of the same machine asked again.
    for listing in filter(None, [system, *(components or [])]):
        record_identity_alias(run, listing, by=by)
    attested = False
    # An embargoed run is skipped here, and that guard was missing.
    #
    # This path exists for the approve-first-link-second case, where the coupling has to be
    # applied now because approval already went by without a listing to apply it to. It read
    # only ``status == APPROVED``, so a reviewer touching the assignment on an approved *held*
    # run certified and published it immediately - the exact early publication the embargo
    # exists to prevent.
    #
    # Reachable before, and much more so since the withhold controls started posting to this
    # endpoint: a reviewer *imposing* an embargo on an approved run would have published its
    # certification in the same request.
    #
    # ``released`` covers the opposite edit: clearing a hold already ran the whole coupling
    # through ``release_held_run``, so doing it again here would be duplicate work.
    if run.status == TestRun.STATUS_APPROVED and not run.is_embargoed and not released:
        ensure_component_ties(run)
        # Same order as approve_run: the release row has to exist before anything
        # can attest to it.
        record_compatibility(run)
        record_architecture(run)
        attested = _apply_attestation(run)
    log_action(
        "test_run.assign_listing",
        target=run,
        actor=by,
        after={
            "listing_system": system.pk if system else None,
            "listing_components": sorted(c.pk for c in components) if components else [],
            "level": level,
            "attested": attested,
        },
    )
    return run


@transaction.atomic
def create_listings_from_run(run: TestRun, *, by) -> list:
    """Create catalog listings from a run's recorded hardware identity.

    The reviewer path for hardware that is not in the catalog yet: a prebuilt
    machine becomes a System listing named by its vendor model; a custom
    build becomes Component listings for its motherboard and CPU. Existing
    listings are reused (matched on vendor + name), the run is linked, and if
    it is already approved the certification coupling is applied - so this
    one action takes an approved run all the way onto the catalog page.

    Listings start unpublished; a passing approved validate run publishes
    them via the attestation step, identical to the pre-approval flow.
    """
    linked = []
    proposal = run.listing_proposal or {}

    # A scoped run takes its own branch and never reaches the machine ones.
    #
    # Reproduced before this existed, with EC2 DMI on a GPU-scoped run: the prebuilt branch below
    # created ``Vendor("Amazon EC2")`` and ``System("m5.large")``, linked the run to it, and left a
    # rented instance type sitting in the hardware catalog as a machine. The card was the only
    # thing anybody had validated.
    #
    # ``ensure_component_ties`` is the whole implementation, because it already resolves the parts
    # a run is evidence for, creates what is missing, and honors the reviewer's exclusions and
    # corrections. ``component_tie_targets`` has already narrowed those parts to the claimed kinds.
    if run.is_scoped:
        ensure_component_ties(run)
        linked = [(component, False) for component in run.listing_components.all()]
        if not linked:
            raise ReviewError(
                "This run is evidence for its "
                f"{' and '.join(run.scope_labels)}, and nothing in it identifies one. "
                "There is nothing to certify."
            )
        attested = False
        if run.status == TestRun.STATUS_APPROVED and not run.is_embargoed:
            attested = _apply_attestation(run)
        log_action(
            "test_run.create_listings", target=run, actor=by,
            after={
                "scope": run.claim_scope,
                "listings": [
                    {"pk": listing.pk, "name": listing.name, "created": created}
                    for listing, created in linked
                ],
                "attested": attested,
            },
        )
        return [listing for listing, _ in linked]

    # The submitter's answer wins whenever they gave one. The detected kind is
    # a heuristic over firmware strings: a vendor that stamps its machine-type
    # code into both DMI tables reads as a custom build, and a barebones
    # chassis with a model name reads as a vendor system. Whoever is holding
    # the machine knows which it is, and a reviewer still approves the result.
    effective_kind = run.effective_system_kind
    # No "unknown" branch: a machine is claimed to be a vendor-built system or it is not, and
    # "not" is a custom build.
    #
    # One thing the third kind did protect, though, and it is worth keeping. When the firmware
    # identifies nothing at all, the submitter's answer is the only identity there is - and
    # "Lenovo / ThinkSystem SR645" with no stated kind would be filed as a *motherboard* called
    # ThinkSystem SR645, because custom is the fallback. That is a wrong entry in the wrong half
    # of the catalog and a nuisance to undo.
    #
    # So the refusal survives, keyed on the data rather than on a classification: nothing here
    # names the machine, and nobody has said which half their answer belongs in. The form asks
    # for it whenever this is the case (``_subject_for`` returns "machine", where the radio is
    # required), so a submitter going through the interface never meets this.
    if (
        is_placeholder(run.board_vendor) or is_placeholder(run.board_model)
    ) and not (proposal.get("machine_kind") or "").strip():
        raise ReviewError(
            "Nothing in this run identifies the machine, and the submitter has not said "
            "whether it is a vendor system or a custom build."
        )

    if effective_kind == SystemKind.PREBUILT:
        vendor_name = proposal.get("vendor_name") or run.system_vendor
        product = proposal.get("name") or run.system_product
        if not (vendor_name and product):
            raise ReviewError("The run has no system vendor/model to create from.")
        vendor = _vendor_for(vendor_name)
        # The reported identity is checked first, so a second run of the same
        # machine joins the listing the first one produced even when the two
        # proposals were typed differently. Without this, approving three
        # back-to-back runs created up to three listings for one machine.
        # Reuse an existing listing unless the submitter has said this is not that machine.
        # Both lookups are skipped when disputed, the identity one especially: it matches on
        # the *reported* strings, which are exactly what was wrong, so it would re-attach the
        # listing being disputed however the submitter renamed the machine.
        system = None
        if not run.identity_disputed:
            system = resolve_reported_system(run) or System.objects.filter(
                vendor=vendor, name__iexact=product
            ).first()
        created = False
        if system is None:
            system = System.objects.create(
                vendor=vendor,
                name=product,
                model_number=(proposal.get("model_number")
                              or run.system_model_number or ""),
                description=proposal.get("description", ""),
                vendor_spec_url=proposal.get("vendor_spec_url", ""),
                created_by=run.submitter,
                # A vendor submitting **their own** hardware becomes the listing's
                # maintainer, which is what gives them edit rights later. Only their own:
                # the attributed vendor has to be the one that made this machine.
                #
                # It used to be ``run.on_behalf_of`` unconditionally, on the assumption that
                # attribution and manufacture were the same thing. They are not, and the
                # consequence was self-reinforcing: a run attributed to Intel created a
                # *Dell* OptiPlex listing owned by Intel, and because ``identity_vendors``
                # counts a maintainer as a company behind the listing, Intel then looked
                # like a legitimate attribution target for that machine forever after. Found
                # by someone asking why Intel was in the dropdown for a Dell system.
                owner_vendor=(
                    run.on_behalf_of
                    if run.on_behalf_of_id and run.on_behalf_of_id == vendor.pk
                    else None
                ),
            )
            created = True
        run.listing_system = system
        run.save(update_fields=["listing_system"])
        if created:
            apply_proposal_metadata(run, system)
        else:
            # An existing listing keeps everything it has, except the two fields its own
            # vendor is allowed to maintain. Without this the vendor's edits were accepted
            # by the form, stored on the run, and then dropped on the floor: description and
            # spec URL are written at ``System.objects.create`` time and nowhere else.
            apply_vendor_maintained_fields(run, system)
        # Recorded whether created or reused: reusing means a human matched a
        # reported identity to an existing listing, which is exactly the
        # mapping worth keeping.
        record_identity_alias(run, system, by=by)
        linked.append((system, created))
        # If the submitter renamed the vendor (DMI said "HPE", the human
        # wrote "Hewlett Packard Enterprise"), record the DMI string as an
        # alias so the next run of this hardware auto-links instead of
        # asking again.
        dmi_vendor = (run.system_vendor or "").strip()
        if dmi_vendor and resolve_vendor(dmi_vendor) is None:
            VendorAlias.objects.get_or_create(name=dmi_vendor, defaults={"vendor": vendor})
    elif effective_kind == SystemKind.CUSTOM:
        from lumina.results.component_match import find_or_create_component

        # The submitter's answer wins over DMI, same as for a prebuilt system:
        # they can see the board, and on an unbranded machine DMI reported
        # nothing usable to begin with.
        board_vendor = proposal.get("vendor_name") or run.board_vendor
        board_model = proposal.get("name") or run.board_model
        if not (board_vendor and board_model):
            raise ReviewError(
                "The run has no motherboard vendor/model to create from."
            )
        board, board_created = find_or_create_component(
            _vendor_for(board_vendor), board_model,
            ComponentKind.motherboard, created_by=run.submitter,
        )
        if board is not None:
            if board_created:
                # Descriptive fields only a human could supply.
                board.description = proposal.get("description", "")
                board.vendor_spec_url = proposal.get("vendor_spec_url", "")
                board.owner_vendor = run.on_behalf_of
                board.save(update_fields=["description", "vendor_spec_url",
                                          "owner_vendor"])
            if board_created:
                apply_proposal_metadata(run, board)
            record_identity_alias(run, board, by=by)
            run.listing_components.add(board)
            linked.append((board, board_created))
        if run.cpu_model:
            brand = CPU_VENDOR_NAMES.get(
                (run.cpu_vendor or "").lower(), run.cpu_vendor or "Unknown"
            )
            cpu, cpu_created = silicon_component(
                _vendor_for(brand), run.cpu_model, ComponentKind.cpu,
                created_by=run.submitter,
            )
            if cpu is not None:
                run.listing_components.add(cpu)
                linked.append((cpu, cpu_created))
        # ``tieable_gpus`` rather than the raw list: it applies the driver rule, resolves the
        # names through ``gpu_identity``, and handles the integrated-GPU case. Reading the summary
        # directly here was a second copy of all of that, and it stopped finding a model at all
        # once the collector started reporting lspci's strings verbatim.
        for gpu_info in tieable_gpus(run):
            brand = gpu_info["vendor"]
            attrs = {
                key: value
                for key, value in (
                    ("driver", gpu_info.get("driver")),
                    ("driver_version", gpu_info.get("driver_version")),
                )
                if value
            }
            gpu_comp, gpu_created = silicon_component(
                _vendor_for(brand), gpu_info["model"], ComponentKind.gpu,
                created_by=run.submitter, extra_attributes=attrs,
            )
            if gpu_comp is not None:
                run.listing_components.add(gpu_comp)
                linked.append((gpu_comp, gpu_created))
        if not linked:
            raise ReviewError(
                "The run recorded no motherboard, CPU, or GPU to create from."
            )
    else:
        raise ReviewError(
            "The run's machine kind is unknown; assign a listing manually instead."
        )

    attested = False
    # ``not is_embargoed`` for the same reason ``approve_run`` withholds: attesting publishes the
    # listing, and unreleased hardware must not reach the catalog on the strength of a run nobody
    # is allowed to see. ``publish_due_runs`` applies it on the release date instead.
    #
    # The guard was only in ``approve_run``, one branch away - and ``approve_run`` sets the status
    # to approved and saves *before* calling this, so an embargoed run arrived here already
    # qualifying. It looked harmless because a brand-new listing has no ``ListingVersion`` yet and
    # ``_attest_one`` bails without one. Reuse is where it bit: an embargoed engineering-sample run
    # whose DMI product differs from the shipping name ("PowerEdge R790 EVT-3") is not auto-linked
    # at ingest, so it reaches this function, matches the existing "PowerEdge R790" by name, finds
    # that listing's release row, and publishes it.
    if run.status == TestRun.STATUS_APPROVED and not run.is_embargoed:
        attested = _apply_attestation(run)
    log_action(
        "test_run.create_listings",
        target=run,
        actor=by,
        after={
            "listings": [
                {"pk": listing.pk, "name": listing.name, "created": created}
                for listing, created in linked
            ],
            "attested": attested,
        },
    )
    return [listing for listing, _ in linked]


# Which test categories can be evidence for which kind of component.
#
# Deliberately narrow and explicit. A claim is only as good as the tests behind it, so a kind with
# no category here cannot be evidenced and cannot be certified, which is the safe direction to be
# wrong in. Adding a kind means writing tests for it first, not adding a line here.
CLAIM_EVIDENCE_CATEGORIES = {
    ComponentKind.cpu.value: {"cpu"},
    ComponentKind.gpu.value: {"gpu"},
    ComponentKind.nic.value: {"network"},
    ComponentKind.storage.value: {"storage"},
    ComponentKind.management.value: {"ipmi"},
}


def unevidenced_claims(run: TestRun) -> list[str]:
    """Kinds this run claims and has no gating evidence for.

    The other half of the safety property, and the half that is easy to miss. ``verdict()`` answers
    "did anything fail", and it says True when nothing failed *because nothing ran*. The only GPU
    validation that existed when scoping was added was ``validate.gpu.driver``, which is
    informational on purpose, so a GPU-scoped run would have come out PASS on the strength of
    having seen a driver and minted a vendor-tier attestation on the card.

    So certifying a claim needs a result that could have failed and did not: non-informational,
    passed, and in a category that pertains to the claimed kind. A run that has none is still
    recorded, and still readable, and certifies nothing.
    """
    if not run.is_scoped:
        return []
    evidenced = set(
        run.results.exclude(severity=Severity.INFORMATIONAL)
        .filter(status=ResultStatus.PASS)
        .values_list("category", flat=True)
    )
    return [
        kind for kind in run.claim_scope
        if not (CLAIM_EVIDENCE_CATEGORIES.get(kind, set()) & evidenced)
    ]


def certifies(run: TestRun) -> bool:
    """Whether this run may move any listing's standing.

    One predicate for the three writers - attestation, release compatibility, and the architecture
    facet - which each carried their own copy of the first two conditions and would each have
    needed their own copy of the third.
    """
    if run.run_type != RunType.validate.value:
        return False
    if run.verdict() is not True:
        return False
    return not unevidenced_claims(run)


def scoped_listings(run: TestRun) -> list:
    """The listings a run is actually evidence for.

    Everything it is tied to, for an ordinary whole-machine run. For a scoped run, only the
    components of the kinds it claims, and never the System: see ``_apply_attestation`` for why.

    One function because three callers need the same answer, and three copies of a rule about what
    a run may certify is two copies too many.
    """
    listings = []
    if run.listing_system_id is not None and not run.is_scoped:
        listings.append(run.listing_system)
    for component in run.listing_components.all():
        if run.is_scoped and component.kind not in run.claim_scope:
            continue
        listings.append(component)
    return listings


def _apply_attestation(run: TestRun) -> bool:
    """Feed an approved, passing validation run into its listings' standing.

    Only validation runs count, only when they are linked to at least one
    listing and actually passed. Prebuilt machines attest their System
    listing; custom builds attest each linked Component (motherboard, CPU).
    The level is capped by what the *submitter* is entitled to claim, using
    the same rule as the human submission flow, so an automated run can
    never grant more trust than its submitter has.

    Repeat runs from the same submitter update evidence rather than
    inflating a listing's attestation count - one submitter is one
    independent confirmation however many times they run the suite.
    """
    # ``certifies`` rather than the two conditions it starts with. This writer used to re-derive a
    # subset - run_type and verdict - and omit the third, ``unevidenced_claims``, which the
    # predicate's own docstring says it exists to unify across the three standing-writers. The gap
    # was real: a scoped run whose only in-scope result was informational passed ``verdict`` and,
    # if the component already carried a release row, minted an attestation up to vendor tier on
    # evidence that proved nothing. record_compatibility and record_architecture already gate on
    # ``certifies``; this was the one writer that never got converted.
    if not certifies(run):
        return False

    attested_any = False
    # The safety property of a scoped run, in one place: ``scoped_listings``.
    #
    # A run scoped to the GPU is evidence that *that card* works on this AlmaLinux release. It is
    # not evidence about the machine around it, and when that machine is a rented cloud instance
    # there is nothing there to be evidence about: the chassis is a hypervisor's idea of one and
    # the next boot is different hardware. So a scoped run certifies components of the kinds it
    # claims, and no System, whatever else it happens to have collected.
    #
    # Enforced here rather than by being careful upstream, because this is the only function that
    # can raise a listing's standing. Every path that reaches the catalog goes through it, so a
    # future caller cannot forget the rule.
    for listing in scoped_listings(run):
        attested_any |= _attest_one(run, listing)
    return attested_any


def effective_level(run: TestRun, listing) -> str:
    """The tier a run's evidence counts as for ``listing``.

    A run that claims nothing gets the highest tier its submitter is entitled
    to, rather than defaulting to community. An explicit claim is honored only if
    the submitter is entitled to it, and is otherwise capped - a run can never
    grant more trust than the person who submitted it has.

    **Attribution is the run's own ``on_behalf_of``, not the listing's owner.**
    Falling back to ``listing.owner_vendor`` conflated two different things: a
    Foundation certifier validating a Dell machine was treated as submitting for
    Dell purely because Dell owns the listing, so their run came out
    vendor-validated when it should have said AlmaLinux. Whose validation this is
    is a fact about the submission, and the submission is where it is stated.

    **And the vendor claim is capped per listing.** A vendor tier says "the company that
    makes this thing validated it", so it can only hold for listings that company actually
    makes. One run touches several: a Dell PowerEdge, Dell's board, Intel's CPU family,
    NVIDIA's GPU. Resolving the tier once per *run* gave all four ``vendor`` on Dell's word,
    so the Intel and NVIDIA family pages read "Vendor-validated" on the strength of a run
    those companies had nothing to do with. Measured before fixing:

        Component  NVIDIA L40S                        level=vendor
        Component  Intel Xeon Scalable 4th Generation level=vendor
        Component  Dell Inc. 0M83RH                   level=vendor
        System     Dell Inc. PowerEdge R760           level=vendor

    Where the attributed vendor is not this listing's, the claim falls back to what the
    submitter is entitled to on their own: ``almalinux`` for a Foundation certifier,
    community otherwise. ``derive_allowed_levels`` never returns the vendor tier without a
    named vendor, so passing ``None`` is exactly that fallback rather than a special case.

    This is the per-listing rule the note above anticipated. It does not reintroduce what
    that note warns against: the listing's vendor can only ever *withhold* the tier here,
    never grant one the submission did not claim.
    """
    # A part may carry its own claim, made on the component rather than on the run: "certify
    # this Xeon family as Intel". That is the only place a component's tier can be set, and it
    # is deliberately separate from the machine's attribution - Intel validating their silicon
    # inside a Dell chassis says nothing about the chassis.
    attributed = _component_claim(run, listing) or run.on_behalf_of
    if attributed is not None and not _listing_belongs_to(listing, attributed):
        attributed = None
    return resolve_claimed_level(
        run.submitter,
        vendor=attributed,
        claimed=run.claimed_validation_level,
    )


def claimable_vendor_for(run: TestRun, component):
    """The vendor whose own certification this run may claim for ``component``, or None.

    One predicate with two readers, and it has to stay that way. The form offers "Certify as
    Intel" only where this returns a vendor, and the engine treats an *unanswered* box as a claim
    only where it does too. They were separate rules, and they disagreed: the form rendered three
    ticked boxes on a run whose engine read no claim at all.

    Three conditions, all from the form's original version. The part must resolve to a catalog
    component; that component's vendor must be verified, since an unverified vendor cannot hand
    out its own tier; and the run's **submitter** must represent that vendor with a submit role -
    not whoever is looking at the page, because ``effective_level`` re-derives the tier from the
    submitter's standing at approval and a claim by anybody else would silently come out
    community.
    """
    from lumina.vendors.models import VendorMembership

    vendor = getattr(component, "vendor", None)
    if component is None or vendor is None or not vendor.verified:
        return None
    submitter = getattr(run, "submitter", None)
    if submitter is None or not getattr(submitter, "is_authenticated", False):
        return None
    if not VendorMembership.objects.filter(
        user=submitter, vendor=vendor, role__in=VendorMembership.SUBMIT_ROLES,
    ).exists():
        return None
    return vendor


def _component_claim(run: TestRun, listing):
    """The vendor a per-part claim names for ``listing``, or None.

    Stored in ``component_overrides`` beside that part's other corrections, keyed by tie. The
    listing is mapped back to its tie by asking the preview which component each tie resolves
    to, rather than by name: a family and a model can share a vendor and a tie key says which
    part was actually meant.

    Grants nothing by itself. The caller still checks that the named vendor is this listing's,
    and ``resolve_claimed_level`` still caps by what the *submitter* may act for, so a claim
    naming a vendor they do not represent comes out community.
    """
    overrides = run.component_overrides or {}
    for entry in preview_component_ties(run):
        component = entry["component"]
        if component is None or component.pk != listing.pk or (
            type(component) is not type(listing)
        ):
            continue
        chosen = overrides.get(entry["key"])
        stated = (
            chosen.get("attribute_to") if isinstance(chosen, dict) else None
        )
        if stated is not None:
            # An explicit answer, either way. "" is a decline the reader gave, and it wins over
            # the default - that distinction is why the decline is stored at all.
            # ``vendor_by_slug``: the form stores ``vendor.slug`` and a slug is not a name.
            return vendor_by_slug(stated) if stated else None
        # Nobody answered, and the box is offered ticked - so the claim holds.
        #
        # Not a loosening of policy: the control has rendered ticked since the vendor claim moved
        # onto the part, and this is the engine catching up with it.
        #
        # Reported twice. A submitter who never opens the listing-details form, or opens it and
        # submits from the run page without saving, has three "Certify as Intel" boxes shown
        # ticked and nothing recorded; the engine read that absence as "no claim" and certified
        # their own parts at community. A default that only takes effect if you press Save is not
        # a default.
        #
        # Grants nothing on its own: ``effective_level`` still caps by what the submitter may act
        # for, and the caller still checks the vendor owns this listing.
        return claimable_vendor_for(run, listing)
    return None


def _listing_belongs_to(listing, vendor) -> bool:
    """Whether ``vendor`` is the company behind ``listing``.

    Manufacturer or maintainer: a listing the community catalogued has no ``owner_vendor``,
    so requiring that alone would deny a vendor their own hardware.
    """
    return vendor.pk in {
        pk for pk in (listing.vendor_id, listing.owner_vendor_id) if pk is not None
    }


def _version_for(run: TestRun, listing) -> ListingVersion | None:
    """The listing's row for the AlmaLinux release this run proved.

    ``record_compatibility`` creates it, and runs first in both callers, so by the
    time we get here it exists for any run that reports a release we recognise.

    Returns None when the run's reported version matches no ``AlmaLinuxRelease``.
    An attestation is a statement about a specific major, so with no major there
    is nothing to state - narrower than the old behaviour, where such a run still
    lifted the listing's tier. Logged rather than silent, because a reviewer who
    approved it would otherwise have no way to see that nothing was certified.
    """
    if run.alma_release_id is None:
        log_action(
            "test_run.attestation_skipped",
            target=run,
            after={"listing": str(listing), "reason": "unrecognised AlmaLinux release"},
        )
        return None
    return ListingVersion.objects.filter(
        release_id=run.alma_release_id, **listing_fk(listing)
    ).first()


def _attest_one(run: TestRun, listing) -> bool:
    """Record this run as one counted attestation for its AlmaLinux release.

    Deduped per **(release, person)**, not per (listing, person). The old rule
    threw away exactly the evidence this design exists to collect: someone who had
    validated a machine on 8 and later validated it on 10 got nothing for the
    second run, so a community proof that older hardware still works on a newer
    AlmaLinux was discarded.

    A repeat run by the same person on the same release stays one attestation. If
    that repeat carries a *higher* tier - they have since joined the vendor - it
    upgrades their own statement rather than adding a second one, so the count
    still reflects people rather than runs.
    """
    try:
        level = effective_level(run, listing)
    except PermissionError:
        return False

    version = _version_for(run, listing)
    if version is None:
        return False

    attestation, created = CommunityAttestation.objects.get_or_create(
        version=version,
        attested_by=run.submitter,
        defaults={"test_run": run, "level": level, **listing_fk(listing)},
    )
    if not created and level_outranks(level, attestation.level):
        attestation.level = level
        attestation.save(update_fields=["level"])

    listing.published = True
    listing.save(update_fields=["published"])
    # Derives this release's tier, the listing's rollup, and its total count.
    recompute_listing_levels(listing)
    return created


def run_trust_level(run: TestRun, listing) -> str:
    """The trust tier a run's evidence counts as for ``listing``.

    Prefers the level frozen on the attestation at approval time; falls back
    to deriving it live (runs that show as evidence without their own
    attestation row, e.g. a submitter's repeat run deduped against their
    first).
    """
    link = listing_fk(listing)
    attestation = run.attestations.filter(**link).first()
    if attestation:
        return attestation.level
    try:
        return effective_level(run, listing)
    except PermissionError:
        return ValidationLevel.COMMUNITY


def publish_due_runs(*, today=None) -> list[TestRun]:
    """Release approved runs whose embargo date has arrived.

    This is also where an embargoed run's certification lands. ``approve_run``
    withholds the coupling so an unreleased machine never reaches the catalog
    early, which makes the release date the moment the listing appears, its
    components tie, and its compatibility is recorded - all at once, as if the
    run had been approved today.
    """
    today = today or timezone.localdate()
    due = list(
        TestRun.objects.filter(
            status=TestRun.STATUS_APPROVED,
            published_at__isnull=True,
            publish_requested_date__isnull=False,
            publish_requested_date__lte=today,
        )
    )
    for run in due:
        release_held_run(run)
    return due


def release_held_run(run: TestRun) -> bool:
    """Publish an approved run that was being withheld, applying its certification now.

    One implementation, two callers. A date arriving is one way a hold ends
    (``publish_due_runs``); a reviewer clearing the embargo by hand is the other, and it has to
    be - a hold with no date waits for a person by definition, so without this the run would
    stay invisible for ever.

    Idempotent: a run that is already published is left alone rather than re-attested.
    """
    if run.published_at is not None or run.status != TestRun.STATUS_APPROVED:
        return False
    now = timezone.now()
    run.published_at = now
    run.save(update_fields=["published_at"])
    ensure_component_ties(run)
    # Same order as approve_run, and for the same reason: the release row has
    # to exist before anything can attest to it.
    record_compatibility(run)
    record_architecture(run)
    attested = _apply_attestation(run)
    log_action("test_run.publish", target=run,
               after={"published_at": now.isoformat(),
                      "attestation_created": attested})
    return True


def submission_preview(run: TestRun, user) -> dict:
    """Everything needed to say what approving this run would change, as plain data.

    Feeds the live summary on the propose-listing form. Computed server-side because only
    the server knows the current catalog: which names already exist, what the listing says
    today, which releases it already claims, and whether this person has already attested a
    given release.

    Shaped for JSON and for a reader, not for the ORM. Every value is a string, number, or
    bool so the template can hand it straight to ``json_script``.
    """
    from lumina.hardware.models import Component, System

    listing = existing_listing_for(run)
    versions = {}
    if listing is not None:
        for version in listing.versions.select_related("release"):
            versions[str(version.release.major)] = {
                "source": version.source,
                # Whether this person has already attested this release. Approving a
                # second run of theirs on the same major adds evidence but not a second
                # confirmation, and a summary that promised one would be wrong.
                "mine": version.attestations.filter(attested_by=user).exists(),
                "attestations": version.attestations.count(),
            }

    return {
        # None when this run creates the listing, which the summary words differently:
        # there is no "before" to diff against.
        "listing": None if listing is None else {
            "label": str(listing),
            "url": listing.get_absolute_url() if hasattr(listing, "get_absolute_url")
                   else "",
            "name": listing.name,
            "model_number": listing.model_number,
            "description": listing.description,
            "vendor_spec_url": listing.vendor_spec_url,
            "vendor": listing.vendor.name if listing.vendor_id else "",
            "published": listing.published,
        },
        "versions": versions,
        # The release this run itself passed on. It is evidence rather than a claim, so the
        # summary says so even when the box for that major is not ticked.
        "run": {
            "major": run.alma_release.major if run.alma_release_id else None,
            "minor": run.alma_minor,
            "passed": run.verdict() is True,
        },
        # Names already in the catalog, so the summary can tell "matches an existing entry"
        # from "creates a new one" as the submitter types. Cased as stored; the comparison
        # is case-insensitive in the browser.
        "known": {
            "vendor": sorted(
                Vendor.objects.values_list("name", flat=True).distinct()
            ),
            "system": sorted(
                System.objects.values_list("name", flat=True).distinct()
            ),
            "component": sorted(
                Component.objects.values_list("name", flat=True).distinct()
            ),
        },
        "components": [
            {
                "key": entry["key"],
                "kind": entry["kind"],
                "kind_label": entry["kind_label"],
                "label": " ".join(
                    part for part in (
                        entry["brand"],
                        entry["raw_model"] or (
                            entry["family"].name if entry["family"] else ""
                        ),
                    ) if part
                ),
                "will_create": entry["will_create"],
                "new_vendor": entry["new_vendor"],
                "matches": str(entry["component"]) if entry["component"] else "",
            }
            for entry in preview_component_ties(run)
        ],
    }
