"""Correcting what a report says a component is, from either seat.

Reported: "Components have vendors and models as well. On the submission form we should
probably be able to tweak those - and also on the review form. We need to of course try to
match existing vendors and models."

The reason it matters is what the report actually says. DMI names a whitebox board's vendor
"OEM" and lspci calls a UHD Graphics 630 "CometLake-S GT2 [UHD Graphics 630]". Approving a
run creates catalog entries from those strings, so uncorrected they become a manufacturer
named OEM and a component nobody will ever search for. The submitter is holding the machine
and the reviewer can see the whole submission; both can tell, and neither could say so.

**A refactor had to come first.** ``component_tie_targets``' docstring claimed to be the one
source for both the preview and the tie, "so the reviewer's preview cannot drift from what
approving actually does" - and ``ensure_component_ties`` re-derived the same board/CPU/GPU
triples itself. Exclusions had already had to be written into both. Overrides would have made
three copies of the same rule, and the first disagreement between them turns the preview into
a lie. ``ensure_component_ties`` now iterates the targets, which is what makes one override
apply to both.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import Group, User
from django.urls import reverse

from lumina.hardware.models import Component, ComponentKind
from lumina.results import ingest, services
from lumina.results.forms import RunComponentTiesForm, RunListingProposalForm
from lumina.results.models import TestRun
from lumina.results.tests import factories as f
from lumina.results.tests.helpers import release
from lumina.vendors.models import Vendor

pytestmark = pytest.mark.django_db


@pytest.fixture
def submitter():
    return User.objects.create_user("corr-sub", password="pw")


@pytest.fixture
def reviewer(client):
    user = User.objects.create_user("corr-rev", password="pw")
    group, _ = Group.objects.get_or_create(name="reviewer")
    user.groups.add(group)
    return user


def _run(submitter):
    return ingest.ingest_bundle(
        submitter=submitter, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"],
            results=[f.validate_result("validate.cpu.functional")],
        ))),
    )


def _run_with_bracketed_gpu(submitter):
    """A run reporting the lspci shape: die name, product in brackets.

    The default fixture's GPU is a bare "L40S", which needs no translating - so a test using
    it would pass whatever the translation did.
    """
    inventory = f.default_inventory()
    inventory["summary"]["gpus"] = [{
        "vendor": "intel", "model": "CometLake-S GT2 [UHD Graphics 630]",
        "driver": "i915", "driver_version": "1.0", "pci": "00:02.0",
    }]
    return ingest.ingest_bundle(
        submitter=submitter, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"], inventory=inventory,
            results=[f.validate_result("validate.cpu.functional")],
        ))),
    )


def _entry(run, kind):
    return next(
        e for e in services.preview_component_ties(run) if e["kind"] == kind
    )


def _fields(run, submitter, kind):
    """The (brand, model) field names for one component on the submitter's form."""
    form = RunListingProposalForm(run=run, user=submitter)
    index = next(
        i for i, row in enumerate(form.component_rows) if row["kind"] == kind
    )
    return (
        f"{form.COMPONENT_BRAND_PREFIX}{index}",
        f"{form.COMPONENT_MODEL_PREFIX}{index}",
    )


# --- a matched part suggests its match, and hides the boxes ----------------------
#
# Reported: "this same concept really holds true for components. If we match it, let's suggest
# the match and not, by default, give them the entry fields. There needs to be an option for the
# user to override of course, just like with the whole system."
#
# Same reasoning as the machine's identity, and the same failure mode it prevents: two prefilled
# boxes over an entry the catalog already holds read as an invitation to retype it, and one
# stray character mints a near-duplicate beside the real thing.


# The default fixture already matches on the CPU and not on the board or the GPU:
#
#     motherboard  brand='Dell Inc.'  component=None
#     cpu          brand='Intel'      component=Intel Intel Xeon Scalable 4th Generation
#     gpu          brand='NVIDIA'     component=None
#
# So the CPU row is the locked case and the board is the new one, with nothing to seed for
# either. Measured rather than assumed: the first version of these tests created a CPU entry of
# its own and asserted the board was matched, both backwards.
MATCHED_KIND = "cpu"
UNMATCHED_KIND = "motherboard"


def _row(run, user, kind):
    form = RunListingProposalForm(run=run, user=user)
    index = next(i for i, r in enumerate(form.component_rows) if r["kind"] == kind)
    return form, index, form.component_rows[index]


def test_a_matched_part_locks_its_boxes(submitter):
    run = _run(submitter)
    assert _entry(run, MATCHED_KIND)["component"] is not None, "the premise"

    _, _, row = _row(run, submitter, MATCHED_KIND)

    assert row["locked"] is True
    assert row.get("edit_field") is not None


def test_a_new_part_still_gets_the_boxes(submitter):
    """Describing it is the whole point there, and nothing exists to suggest."""
    run = _run(submitter)
    assert _entry(run, UNMATCHED_KIND)["component"] is None, "the premise: not in the catalog"

    _, _, row = _row(run, submitter, UNMATCHED_KIND)

    assert row["locked"] is False
    assert row.get("edit_field") is None


def test_the_boxes_are_collapsed_on_the_page(client, submitter):
    """Rendered but hidden, so the override can reveal them with no round trip - the same
    mechanism, and the same reason, as the machine's identity fields."""
    run = _run(submitter)
    client.force_login(submitter)

    body = client.get(reverse("results:propose_listing", args=[run.uuid])).content.decode()

    assert "Not this part? Correct it" in body
    assert "reveal-fields" in body
    brand, _ = _fields(run, submitter, MATCHED_KIND)
    assert f'name="{brand}"' in body, "collapsed, not absent"


def test_leaving_a_matched_part_alone_records_nothing(client, submitter):
    """The point of locking. A row nobody opened must not write a correction, even though its
    boxes post their prefilled values like any other text input."""
    run = _run(submitter)
    brand, model = _fields(run, submitter, MATCHED_KIND)
    client.force_login(submitter)

    client.post(reverse("results:propose_listing", args=[run.uuid]), {
        "vendor_name": "Dell Inc.", "name": "PowerEdge R760", "machine_kind": "prebuilt",
        "components_submitted": "1",
        "included_ties": [e["key"] for e in services.preview_component_ties(run)],
        brand: "Something Else", model: "Not The Match",
    })

    run.refresh_from_db()
    assert run.component_overrides == {}


def test_ticking_the_override_records_the_correction(client, submitter):
    run = _run(submitter)
    form, index, _ = _row(run, submitter, MATCHED_KIND)
    brand, model = _fields(run, submitter, MATCHED_KIND)
    client.force_login(submitter)

    client.post(reverse("results:propose_listing", args=[run.uuid]), {
        "vendor_name": "Dell Inc.", "name": "PowerEdge R760", "machine_kind": "prebuilt",
        "components_submitted": "1",
        "included_ties": [e["key"] for e in services.preview_component_ties(run)],
        f"{form.COMPONENT_EDIT_PREFIX}{index}": "1",
        brand: "AMD", model: "EPYC 7343",
    })

    run.refresh_from_db()
    key = _entry(run, MATCHED_KIND)["key"]
    assert run.component_overrides[key]["model"] == "EPYC 7343"


def test_a_corrected_row_stays_open(client, submitter):
    """So the reader can keep editing, or undo by retyping what the report said. The machine's
    identity behaves the same way once disputed: the fields come back as a plain card.

    The correction has to land on a part the catalog *does* hold, or this proves nothing. The
    first version overrode to a part nobody had listed, so ``component`` was None and the row
    would have read as unlocked however the rule was written - it survived a mutation that
    locked every matched row, which is how it was caught.
    """
    run = _run(submitter)
    # AMD is already in the seeded catalog, so this reuses it rather than colliding on the slug.
    amd, _ = Vendor.objects.get_or_create(name="AMD", defaults={"published": True})
    Component.objects.get_or_create(
        vendor=amd, name="EPYC 7343", kind=ComponentKind.cpu.value,
    )
    run.component_overrides = {
        _entry(run, MATCHED_KIND)["key"]: {"brand": "AMD", "model": "EPYC 7343"},
    }
    run.save(update_fields=["component_overrides"])
    row = _row(run, submitter, MATCHED_KIND)[2]
    assert row["component"] is not None, "the premise: the correction matches an entry"
    assert row["overridden"] is True

    assert row["locked"] is False
    assert row.get("edit_field") is None


def test_the_vendor_claim_is_still_offered_and_ticked(submitter):
    """Explicitly required: "If a vendor of a component submitted the run we still need to
    prompt them, and check the box by default, to upgrade the certification level."

    Locking the boxes must not touch the claim. Certifying a part the catalog already holds is
    the *normal* case for a component vendor - the entry is right, and their validation is what
    is new - so the row that is most likely to be locked is exactly the row that most needs the
    claim.
    """
    from lumina.vendors.models import VendorMembership

    run = _run(submitter)
    vendor = _entry(run, MATCHED_KIND)["component"].vendor
    # Verified, because ``_add_component_claim`` requires it - an unverified vendor cannot hand
    # out its own tier. The seeded Intel is not, which is why the first run of this test found
    # no claim field and looked like the lock had eaten it.
    vendor.verified = True
    vendor.save(update_fields=["verified"])
    VendorMembership.objects.create(
        user=submitter, vendor=vendor, role=VendorMembership.ROLE_SUBMITTER,
    )

    form, index, row = _row(run, submitter, MATCHED_KIND)

    assert row["locked"] is True, "the case under test"
    assert row.get("claim_field") is not None
    assert form.initial[f"{form.COMPONENT_CLAIM_PREFIX}{index}"] is True


def test_the_reviewer_gets_the_same_control(client, submitter, reviewer):
    """One partial, both seats. Rendering the boxes by hand on the review page is what would let
    the two drift into offering different controls over the same data - and if the override were
    missing there, a reviewer's correction of a matched part would silently do nothing."""
    run = _run(submitter)
    client.force_login(reviewer)

    body = client.get(reverse("review:run_detail", args=[run.pk])).content.decode()

    assert "Not this part? Correct it" in body

    form = RunComponentTiesForm(run=run)
    index = next(
        i for i, r in enumerate(form.component_rows) if r["kind"] == MATCHED_KIND
    )
    payload = {
        "components_submitted": "1",
        "included_ties": [e["key"] for e in services.preview_component_ties(run)],
        f"{form.COMPONENT_EDIT_PREFIX}{index}": "1",
        f"{form.COMPONENT_BRAND_PREFIX}{index}": "AMD",
        f"{form.COMPONENT_MODEL_PREFIX}{index}": "EPYC 7343",
    }
    client.post(reverse("review:run_component_ties", args=[run.pk]), payload)

    run.refresh_from_db()
    assert run.component_overrides[_entry(run, MATCHED_KIND)["key"]]["model"] == "EPYC 7343"


# --- the targets carry a stable key ---------------------------------------------


def test_the_key_survives_a_correction(submitter):
    """The key is what an exclusion *and* an override are filed under, so deriving it from
    the corrected model would move the very row somebody was correcting - unpinning the
    earlier decision the moment they made this one."""
    run = _run(submitter)
    before = _entry(run, "gpu")["key"]

    run.component_overrides = {before: {"model": "UHD Graphics 630"}}
    run.save(update_fields=["component_overrides"])

    after = _entry(run, "gpu")
    assert after["key"] == before
    assert after["raw_model"] == "UHD Graphics 630"


def test_the_report_is_kept_alongside_the_correction(submitter):
    """So the form can show what was changed, and a reviewer can see it was changed."""
    run = _run(submitter)
    key = _entry(run, "motherboard")["key"]
    reported = _entry(run, "motherboard")["reported_model"]

    run.component_overrides = {key: {"brand": "ASRock", "model": "B650M"}}
    run.save(update_fields=["component_overrides"])

    entry = _entry(run, "motherboard")
    assert entry["brand"] == "ASRock"
    assert entry["reported_model"] == reported
    assert entry["overridden"] is True


def test_a_blank_override_keeps_what_was_reported(submitter):
    """Blank means "no change", not "erase". An empty vendor box must not tie a component
    to a nameless manufacturer."""
    run = _run(submitter)
    entry = _entry(run, "cpu")
    run.component_overrides = {entry["key"]: {"brand": "", "model": ""}}
    run.save(update_fields=["component_overrides"])

    after = _entry(run, "cpu")
    assert after["brand"] == entry["reported_brand"]
    assert after["raw_model"] == entry["reported_model"]
    assert after["overridden"] is False


# --- the correction reaches the catalog -----------------------------------------


def test_the_corrected_name_is_what_gets_created(submitter, reviewer):
    """The point of the whole feature. Approving creates the entry, so the correction has to
    be what approving sees - which is why ``ensure_component_ties`` had to stop re-deriving
    its own copy of the targets."""
    run = _run(submitter)
    key = _entry(run, "motherboard")["key"]
    run.component_overrides = {key: {"brand": "ASRock", "model": "B650M PG Riptide"}}
    run.save(update_fields=["component_overrides"])

    services.approve_run(release(TestRun.objects.get(pk=run.pk)), by=reviewer)

    assert Component.objects.filter(
        kind=ComponentKind.motherboard.value, name="B650M PG Riptide",
    ).exists()
    assert not Component.objects.filter(name="0M83RH").exists()


def test_a_corrected_vendor_does_not_mint_the_reported_one(submitter, reviewer):
    """The concrete harm: an uncorrected "OEM" becomes a catalog manufacturer named OEM."""
    run = _run(submitter)
    key = _entry(run, "motherboard")["key"]
    run.component_overrides = {key: {"brand": "ASRock"}}
    run.save(update_fields=["component_overrides"])

    services.approve_run(release(TestRun.objects.get(pk=run.pk)), by=reviewer)

    assert Vendor.objects.filter(name="ASRock").exists()


def test_a_correction_can_match_an_existing_component(submitter, reviewer):
    """Matching is the whole reason the boxes suggest catalog names: a correction should
    *reuse* an entry rather than mint a near-duplicate beside it."""
    asrock = Vendor.objects.create(name="ASRock", published=True)
    existing = Component.objects.create(
        vendor=asrock, name="B650M PG Riptide",
        kind=ComponentKind.motherboard.value, published=True,
    )
    run = _run(submitter)
    key = _entry(run, "motherboard")["key"]
    run.component_overrides = {key: {"brand": "ASRock", "model": "B650M PG Riptide"}}
    run.save(update_fields=["component_overrides"])

    services.approve_run(release(TestRun.objects.get(pk=run.pk)), by=reviewer)

    run.refresh_from_db()
    assert existing in run.listing_components.all()
    assert Component.objects.filter(
        kind=ComponentKind.motherboard.value, name="B650M PG Riptide",
    ).count() == 1


def test_the_preview_and_the_tie_agree_on_the_correction(submitter, reviewer):
    """The drift the refactor exists to prevent, asserted directly: whatever the preview
    names is what ends up in the catalog."""
    run = _run(submitter)
    key = _entry(run, "gpu")["key"]
    run.component_overrides = {key: {"brand": "Intel", "model": "UHD Graphics 630"}}
    run.save(update_fields=["component_overrides"])
    previewed = _entry(run, "gpu")["component"]

    services.approve_run(release(TestRun.objects.get(pk=run.pk)), by=reviewer)

    run.refresh_from_db()
    names = {c.name for c in run.listing_components.all()}
    assert str(previewed) in names or previewed is None


# --- the submitter's form -------------------------------------------------------


def test_the_submitter_gets_a_box_per_component(submitter):
    run = _run(submitter)

    form = RunListingProposalForm(run=run, user=submitter)

    # board, CPU, GPU, NIC. The NIC joined the list when the collector learned to name one;
    # its two ports are one row, because a card is one component.
    assert len(form.component_rows) == 4
    for row in form.component_rows:
        assert row["brand_field"] is not None
        assert row["model_field"] is not None


def test_the_boxes_are_prefilled_with_the_current_values(submitter):
    run = _run(submitter)

    form = RunListingProposalForm(run=run, user=submitter)
    row = next(r for r in form.component_rows if r["kind"] == "motherboard")

    assert form.initial[row["brand_field"].name] == row["brand"]
    assert form.initial[row["model_field"].name] == row["raw_model"]


def test_the_boxes_suggest_existing_catalog_names(submitter):
    """Free text, with the catalog offered. Hardware nobody has seen has to be typeable, and
    a near-duplicate is what suggestions are meant to prevent."""
    asrock = Vendor.objects.create(name="ASRock", published=True)
    Component.objects.create(
        vendor=asrock, name="B650M PG Riptide",
        kind=ComponentKind.motherboard.value, published=True,
    )
    run = _run(submitter)

    form = RunListingProposalForm(run=run, user=submitter)
    row = next(r for r in form.component_rows if r["kind"] == "motherboard")

    assert "ASRock" in row["brand_field"].field.widget.options
    assert "B650M PG Riptide" in row["model_field"].field.widget.options


def test_the_suggestions_are_scoped_to_the_kind(submitter):
    """A board box offering CPU names would invite exactly the mistake it is there to stop."""
    intel = Vendor.objects.create(name="Intel Corp", published=True)
    Component.objects.create(
        vendor=intel, name="Some CPU Family", kind=ComponentKind.cpu.value,
    )
    run = _run(submitter)

    form = RunListingProposalForm(run=run, user=submitter)
    board = next(r for r in form.component_rows if r["kind"] == "motherboard")

    assert "Some CPU Family" not in board["model_field"].field.widget.options


def test_posting_a_correction_stores_only_what_changed(client, submitter):
    """Echoing every box back would record a correction on every component the moment
    anybody saved the form, and that would then survive a re-upload that got it right."""
    run = _run(submitter)
    brand_field, model_field = _fields(run, submitter, "motherboard")
    entries = services.preview_component_ties(run)
    payload = {
        "vendor_name": "Dell Inc.", "name": "PowerEdge R760",
        "machine_kind": "prebuilt",
        "included_ties": [e["key"] for e in entries],
        brand_field: "ASRock", model_field: "B650M PG Riptide",
        # The hidden marker a browser posts with this section. Without it the form treats
        # the whole thing as unsubmitted and leaves the component state alone, because an
        # unticked box is indistinguishable from a section that was never rendered.
        "components_submitted": "1",
    }
    # Every other box posted unchanged.
    form = RunListingProposalForm(run=run, user=submitter)
    for index, row in enumerate(form.component_rows):
        payload.setdefault(f"{form.COMPONENT_BRAND_PREFIX}{index}", row["brand"])
        payload.setdefault(f"{form.COMPONENT_MODEL_PREFIX}{index}", row["raw_model"])
    client.force_login(submitter)

    client.post(reverse("results:propose_listing", args=[run.uuid]), payload)

    run.refresh_from_db()
    assert list(run.component_overrides) == [
        next(e["key"] for e in entries if e["kind"] == "motherboard")
    ]


def test_the_correction_does_not_pollute_the_listing_proposal(client, submitter):
    run = _run(submitter)
    brand_field, model_field = _fields(run, submitter, "motherboard")
    client.force_login(submitter)

    client.post(reverse("results:propose_listing", args=[run.uuid]), {
        "vendor_name": "Dell Inc.", "name": "PowerEdge R760",
        "machine_kind": "prebuilt",
        brand_field: "ASRock", model_field: "B650M PG Riptide",
    })

    run.refresh_from_db()
    # Nothing that is a control rather than a fact about the listing. Five keys reached this
    # blob one at a time before the form started declaring which of its fields are which.
    assert not any(
        key.startswith("tie_") for key in run.listing_proposal
    ), run.listing_proposal
    assert "components_submitted" not in run.listing_proposal
    assert "included_ties" not in run.listing_proposal
    assert "attribution" not in run.listing_proposal


def test_the_page_renders_the_boxes(client, submitter):
    run = _run(submitter)
    client.force_login(submitter)

    body = client.get(
        reverse("results:propose_listing", args=[run.uuid])
    ).content.decode()

    assert 'name="tie_brand_0"' in body
    assert 'name="tie_model_0"' in body


# --- the reviewer's form --------------------------------------------------------


def test_the_reviewer_can_correct_a_component(client, submitter, reviewer):
    run = _run(submitter)
    entries = services.preview_component_ties(run)
    board = next(e for e in entries if e["kind"] == "motherboard")
    form = RunComponentTiesForm(run=run)
    index = next(
        i for i, row in enumerate(form.component_rows) if row["kind"] == "motherboard"
    )
    payload = {"included_ties": [e["key"] for e in entries],
               "components_submitted": "1"}
    for i, row in enumerate(form.component_rows):
        payload[f"{form.COMPONENT_BRAND_PREFIX}{i}"] = row["brand"]
        payload[f"{form.COMPONENT_MODEL_PREFIX}{i}"] = row["raw_model"]
    payload[f"{form.COMPONENT_BRAND_PREFIX}{index}"] = "ASRock"
    client.force_login(reviewer)

    resp = client.post(reverse("review:run_component_ties", args=[run.pk]), payload)

    assert resp.status_code == 302
    run.refresh_from_db()
    assert run.component_overrides[board["key"]]["brand"] == "ASRock"


def test_the_reviewer_can_drop_a_component(client, submitter, reviewer):
    """The exclusion they appeared to have and did not: clearing the list through
    ``run_assign_listing`` was undone at approval, because the ties are re-derived."""
    run = _run(submitter)
    entries = services.preview_component_ties(run)
    gpu = next(e for e in entries if e["kind"] == "gpu")
    form = RunComponentTiesForm(run=run)
    payload = {
        "included_ties": [e["key"] for e in entries if e["key"] != gpu["key"]],
        "components_submitted": "1",
    }
    for i, row in enumerate(form.component_rows):
        payload[f"{form.COMPONENT_BRAND_PREFIX}{i}"] = row["brand"]
        payload[f"{form.COMPONENT_MODEL_PREFIX}{i}"] = row["raw_model"]
    client.force_login(reviewer)

    client.post(reverse("review:run_component_ties", args=[run.pk]), payload)

    run.refresh_from_db()
    assert run.excluded_component_ties == [gpu["key"]]


def test_the_reviewers_page_renders_the_marker(client, submitter, reviewer):
    """The reviewer's form shares ``ComponentTiesMixin``, so it needs the same marker. Without
    it their every save reads as "this section was not submitted" and quietly does nothing -
    which is worse than the bug it guards against, and only a rendering test catches it."""
    run = _run(submitter)
    client.force_login(reviewer)

    body = client.get(reverse("review:run_detail", args=[run.pk])).content.decode()

    assert 'name="components_submitted"' in body


def test_a_reviewer_post_without_the_marker_changes_nothing(client, submitter, reviewer):
    run = _run(submitter)
    entries = services.preview_component_ties(run)
    client.force_login(reviewer)

    client.post(reverse("review:run_component_ties", args=[run.pk]), {
        "included_ties": [e["key"] for e in entries[:1]],
    })

    run.refresh_from_db()
    assert run.excluded_component_ties == []


def test_a_plain_user_cannot_edit_the_ties(client, submitter):
    run = _run(submitter)
    client.force_login(submitter)

    resp = client.post(reverse("review:run_component_ties", args=[run.pk]), {})

    assert resp.status_code == 403


def test_the_reviewer_page_renders_the_controls(client, submitter, reviewer):
    # Left as a draft: a reviewer can open any run, and submitting this one would first
    # demand listing details the fixture has no reason to supply.
    run = _run(submitter)
    client.force_login(reviewer)

    body = client.get(reverse("review:run_detail", args=[run.pk])).content.decode()

    assert reverse("review:run_component_ties", args=[run.pk]) in body
    assert 'name="tie_brand_0"' in body
    assert "Components this run is evidence for" in body


def test_both_forms_share_one_implementation():
    """Two editors of "what would approving do" that disagree is the drift the single
    ``component_tie_targets`` source exists to prevent."""
    from lumina.results.forms import ComponentTiesMixin

    assert issubclass(RunListingProposalForm, ComponentTiesMixin)
    assert issubclass(RunComponentTiesForm, ComponentTiesMixin)


# --- showing the translation ----------------------------------------------------
#
# Reported: "We're showing what the CPU will translate to on the proposal page, but not the
# GPU. We should also show what the GPU model will translate to family-wise if applicable."
#
# The CPU appeared to be special only because it happened to *match* something: the card
# said "Matches Intel Core 10th Generation", which is the family. A GPU that matches nothing
# said "New - approving creates this catalog entry" and never said under what name, so the
# translation from "CometLake-S GT2 [UHD Graphics 630]" to "UHD Graphics 630" was invisible
# exactly where it mattered most.
#
# Shown beside the reported string rather than instead of it, which is the same rule as the
# storage layer: the report is what is kept, and the translation is a derived view of it that
# can be corrected later.


def test_the_preview_names_what_a_new_entry_would_be_called(submitter):
    run = _run_with_bracketed_gpu(submitter)

    gpu = _entry(run, "gpu")

    assert gpu["will_create"] is True
    assert gpu["catalog_name"] == "UHD Graphics 630"
    assert gpu["raw_model"] != gpu["catalog_name"], "nothing to show if they match"


def test_the_translation_is_reported_for_every_kind(submitter):
    """Not a CPU privilege. A board has no families to roll up to but still gets a name."""
    run = _run_with_bracketed_gpu(submitter)

    for entry in services.preview_component_ties(run):
        assert "catalog_name" in entry, entry["kind"]
        assert entry["catalog_name"], entry["kind"]


def test_a_gpu_family_is_reported_when_one_is_curated(submitter):
    """The "family-wise if applicable" half. Curated families are what certification
    actually applies to, so a GPU rolling up to one should say so before approval."""
    from lumina.hardware.models import ComponentRole
    from lumina.vendors.services import resolve_vendor

    intel = resolve_vendor("Intel")
    family = Component.objects.create(
        vendor=intel, name="Intel UHD Graphics", kind=ComponentKind.gpu.value,
        role=ComponentRole.FAMILY, model_patterns=[r"UHD Graphics \d+"],
        published=True,
    )
    run = _run_with_bracketed_gpu(submitter)

    gpu = _entry(run, "gpu")

    assert gpu["family"] == family
    assert gpu["will_create"] is False
    assert gpu["component"] == family


def test_the_page_shows_the_name_a_new_component_would_get(client, submitter):
    run = _run_with_bracketed_gpu(submitter)
    client.force_login(submitter)

    body = " ".join(client.get(
        reverse("results:propose_listing", args=[run.uuid])
    ).content.decode().split())

    assert "creates this catalog entry as <strong>UHD Graphics 630</strong>" in body


def test_the_page_shows_the_family_when_there_is_one(client, submitter):
    from lumina.hardware.models import ComponentRole
    from lumina.vendors.services import resolve_vendor

    Component.objects.create(
        vendor=resolve_vendor("Intel"), name="Intel UHD Graphics",
        kind=ComponentKind.gpu.value, role=ComponentRole.FAMILY,
        model_patterns=[r"UHD Graphics \d+"], published=True,
    )
    run = _run_with_bracketed_gpu(submitter)
    client.force_login(submitter)

    body = " ".join(client.get(
        reverse("results:propose_listing", args=[run.uuid])
    ).content.decode().split())

    assert "Certification applies to the family <strong>Intel UHD Graphics</strong>" in body


def test_the_reviewer_page_names_it_too(client, submitter, reviewer):
    run = _run_with_bracketed_gpu(submitter)
    client.force_login(reviewer)

    body = " ".join(client.get(
        reverse("review:run_detail", args=[run.pk])
    ).content.decode().split())

    assert "UHD Graphics 630" in body


def test_the_preview_and_the_creation_agree_on_the_name(submitter, reviewer):
    """``catalog_name`` is extracted from ``find_or_create_component`` rather than
    reimplemented, so a preview cannot promise one name and approval produce another."""
    run = _run_with_bracketed_gpu(submitter)
    promised = _entry(run, "gpu")["catalog_name"]

    services.approve_run(release(TestRun.objects.get(pk=run.pk)), by=reviewer)

    assert Component.objects.filter(
        kind=ComponentKind.gpu.value, name=promised,
    ).exists()


def test_an_unresolved_vendor_does_not_crash_the_preview(submitter):
    """``strip_vendor_prefix`` indexed ``vendor_name.split()[0]``, which raises on an empty
    name. Every previous caller passed a saved Vendor; the preview reports on strings whose
    brand may match nothing in the catalog yet, and 80 tests failed the moment it did."""
    from lumina.results.component_match import catalog_name

    assert catalog_name(None, "GA102 [GeForce RTX 4090]", ComponentKind.gpu) == (
        "GeForce RTX 4090"
    )
    assert catalog_name(None, "", ComponentKind.gpu) == ""


def _run_with_gpu(submitter, vendor, model):
    inventory = f.default_inventory()
    inventory["summary"]["gpus"] = [{
        "vendor": vendor, "model": model, "driver": "nvidia",
        "driver_version": "570.86.15", "pci": "01:00.0",
    }]
    return ingest.ingest_bundle(
        submitter=submitter, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"], inventory=inventory,
            results=[f.validate_result("validate.cpu.functional")],
        ))),
    )


def test_a_seeded_family_is_where_certification_lands(submitter):
    """"Family-wise if applicable" against the real seed data rather than a fixture.

    An RTX 4090 rolls up to the curated "NVIDIA GeForce RTX 40 Series", so the tie is the
    family even though the string also translates to a model name. Both are worth showing and
    they are different facts: ``catalog_name`` is what the string means, the family is where
    certification lands.
    """
    run = _run_with_gpu(submitter, "nvidia", "NVIDIA GeForce RTX 4090")

    gpu = _entry(run, "gpu")

    assert gpu["catalog_name"] == "GeForce RTX 4090"
    assert gpu["family"].name == "NVIDIA GeForce RTX 40 Series"
    assert gpu["will_create"] is False


def test_the_promise_holds_when_the_vendor_prefix_matters(submitter, reviewer):
    """The case that distinguishes ``catalog_name`` from a bare normalizer call.

    With the NVIDIA driver installed, nvidia-smi's ``product_name`` wins and reports
    "NVIDIA GeForce GT 710"; without it lspci reports "GK208B [GeForce GT 710]". One card, and
    ``strip_vendor_prefix`` is what makes them one entry - normalizing alone leaves the first
    as "NVIDIA GeForce GT 710".

    An older GeForce on purpose: the reference-data migration lists those as a deliberate gap,
    so no curated family intercepts this and the created *model* name is observable. Added
    because reimplementing ``catalog_name`` as a bare ``NORMALIZERS[kind]`` call passed every
    other test here - for the Intel string the two happen to agree, so nothing noticed.
    """
    run = _run_with_gpu(submitter, "nvidia", "NVIDIA GeForce GT 710")
    promised = _entry(run, "gpu")["catalog_name"]
    assert promised == "GeForce GT 710", promised

    services.approve_run(release(TestRun.objects.get(pk=run.pk)), by=reviewer)

    run.refresh_from_db()
    names = {c.name for c in run.listing_components.all() if c.kind == "gpu"}
    assert names == {promised}, names


def test_echoing_the_prefill_is_not_a_correction(client, submitter):
    """The box is prefilled with the *resolved* vendor - "NVIDIA" for a reported "NVIDIA
    Corporation" - because that reads better. Comparing only against the reported string recorded
    a correction on every part whose vendor the catalog spells differently, from a reader who
    touched nothing.

    Latent for a long time and invisible while the collector happened to report the short
    spellings itself. It surfaced the moment the collector started reporting pci.ids verbatim.
    """
    run = _run(submitter)
    form = RunListingProposalForm(run=run, user=submitter)
    gpu = next(i for i, row in enumerate(form.component_rows) if row["kind"] == "gpu")
    assert form.component_rows[gpu]["brand"] != form.component_rows[gpu]["reported_brand"], (
        "the premise: the prefill and the reported string differ here"
    )
    payload = {
        "vendor_name": "Dell Inc.", "name": "PowerEdge R760", "machine_kind": "prebuilt",
        "components_submitted": "1",
        "included_ties": [row["key"] for row in form.component_rows],
    }
    for index, row in enumerate(form.component_rows):
        payload[f"{form.COMPONENT_BRAND_PREFIX}{index}"] = row["brand"]
        payload[f"{form.COMPONENT_MODEL_PREFIX}{index}"] = row["raw_model"]
    client.force_login(submitter)

    client.post(reverse("results:propose_listing", args=[run.uuid]), payload)

    run.refresh_from_db()
    assert run.component_overrides == {}


def test_typing_what_the_report_said_is_not_a_correction_either(client, submitter):
    """Which is how an existing override is undone: the prefill is then the override, and the
    reported string is the way back to it."""
    run = _run(submitter)
    form = RunListingProposalForm(run=run, user=submitter)
    gpu_index = next(i for i, row in enumerate(form.component_rows) if row["kind"] == "gpu")
    row = form.component_rows[gpu_index]
    payload = {
        "vendor_name": "Dell Inc.", "name": "PowerEdge R760", "machine_kind": "prebuilt",
        "components_submitted": "1",
        "included_ties": [r["key"] for r in form.component_rows],
        f"{form.COMPONENT_BRAND_PREFIX}{gpu_index}": row["reported_brand"],
        f"{form.COMPONENT_MODEL_PREFIX}{gpu_index}": row["reported_model"],
    }
    client.force_login(submitter)

    client.post(reverse("results:propose_listing", args=[run.uuid]), payload)

    run.refresh_from_db()
    assert row["key"] not in run.component_overrides


def test_a_real_correction_is_still_recorded(client, submitter):
    run = _run(submitter)
    form = RunListingProposalForm(run=run, user=submitter)
    gpu_index = next(i for i, r in enumerate(form.component_rows) if r["kind"] == "gpu")
    key = form.component_rows[gpu_index]["key"]
    payload = {
        "vendor_name": "Dell Inc.", "name": "PowerEdge R760", "machine_kind": "prebuilt",
        "components_submitted": "1",
        "included_ties": [r["key"] for r in form.component_rows],
        f"{form.COMPONENT_BRAND_PREFIX}{gpu_index}": "AMD",
        f"{form.COMPONENT_MODEL_PREFIX}{gpu_index}": "Radeon Pro W7900",
    }
    client.force_login(submitter)

    client.post(reverse("results:propose_listing", args=[run.uuid]), payload)

    run.refresh_from_db()
    assert run.component_overrides[key] == {"brand": "AMD", "model": "Radeon Pro W7900"}
