"""Reviewer-side listing assignment.

Approving a validation run is what puts its hardware in the catalog, so it
creates the listing itself. The "Create listing(s) from this run" button used to
be a separate step, which meant a reviewer could approve a run and quietly
certify nothing by forgetting to press it first.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import Group, User
from django.urls import reverse

from lumina.hardware.models import Component, ComponentKind, ComponentRole, System
from lumina.results import ingest, services
from lumina.results.forms import RunListingAssignForm
from lumina.results.tests import factories as f
from lumina.results.tests.helpers import release
from lumina.vendors.models import Vendor

pytestmark = pytest.mark.django_db


@pytest.fixture
def submitter():
    return User.objects.create_user("assign-sub", email="as@example.com")


@pytest.fixture
def reviewer():
    user = User.objects.create_user("assign-rev", email="ar@example.com")
    group, _ = Group.objects.get_or_create(name="reviewer")
    user.groups.add(group)
    return user


def _pending(submitter, **report_kw):
    Vendor.objects.get_or_create(name="Dell Inc.", defaults={"slug": "dell-inc"})
    report = f.make_report(
        run_types=["validate"],
        results=[f.validate_result("validate.cpu.functional")],
        **report_kw,
    )
    run = ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(f.build_bundle(report)),
        source="api",
    )
    return release(run)


# --- approval creates the listing ----------------------------------------------


def test_approving_creates_the_listing_with_no_separate_step(submitter, reviewer):
    run = _pending(submitter)
    assert run.listing_system is None

    services.approve_run(run, by=reviewer)

    run.refresh_from_db()
    assert run.listing_system == System.objects.get(name="PowerEdge R760")
    assert run.listing_system.published is True


def test_an_assigned_listing_is_reused_not_duplicated(submitter, reviewer):
    """A reviewer who picked an existing entry must not get a second one."""
    dell = Vendor.objects.get_or_create(
        name="Dell Inc.", defaults={"slug": "dell-inc"})[0]
    existing = System.objects.create(vendor=dell, name="PowerEdge R760",
                                     slug="dell-poweredge-r760-x")
    run = _pending(submitter)
    services.assign_listing(run, system=existing, components=[], level="",
                            by=reviewer)

    services.approve_run(run, by=reviewer)

    run.refresh_from_db()
    assert run.listing_system == existing
    assert System.objects.filter(name="PowerEdge R760").count() == 1


def test_a_run_with_no_identity_refuses_approval_with_a_reason(submitter, reviewer):
    """Better than approving something that certifies nothing: the reviewer has
    to say what the hardware is first."""
    inventory = f.default_inventory()
    inventory["summary"]["system"] = {"vendor": None, "product": None,
                                      "kind": "unknown", "bios": {}}
    inventory["summary"]["baseboard"] = {"vendor": None, "product": None}
    run = _pending(submitter, inventory=inventory)

    with pytest.raises(services.ReviewError, match="has not said whether"):
        services.approve_run(run, by=reviewer)


def test_the_button_is_gone_and_the_page_says_what_approval_will_do(
        client, submitter, reviewer):
    run = _pending(submitter)
    client.force_login(reviewer)

    body = client.get(reverse("review:run_detail", args=[run.pk])).content.decode()

    assert "Create listing(s) from this run" not in body
    # Wording moved when the proposal and assignment boxes merged, the claim did not: the page
    # still has to say approving creates a listing, and name the machine it will create.
    assert "Approving creates a listing" in body
    assert "Dell Inc." in body
    assert "PowerEdge R760" in body


def test_the_endpoint_is_gone_too(client, submitter, reviewer):
    """Leaving it reachable would keep a second, divergent path to the same
    outcome."""
    from django.urls import NoReverseMatch

    with pytest.raises(NoReverseMatch):
        reverse("review:run_create_listings", args=[1])


# --- the pickers ---------------------------------------------------------------


def test_the_system_picker_is_searchable_but_still_a_strict_choice(submitter):
    """Free text is wrong here: a reviewer assigning an *existing* listing has
    to land on one that exists. Only the picking gets easier."""
    run = _pending(submitter)
    form = RunListingAssignForm(run=run)

    widget = form.fields["system"].widget
    assert widget.attrs.get("data-combobox") == "true"
    from django import forms as django_forms
    assert isinstance(widget, django_forms.Select)
    assert not isinstance(widget, django_forms.SelectMultiple)


def test_the_component_picker_is_a_search_and_add_list(submitter):
    run = _pending(submitter)
    form = RunListingAssignForm(run=run)

    widget = form.fields["components"].widget
    assert widget.attrs.get("data-picker") == "true"
    assert "components" in widget.attrs.get("data-picker-placeholder", "")
    assert "size" not in widget.attrs      # no scrolling multi-select


def test_already_linked_components_come_back_as_the_initial_value(submitter,
                                                                 reviewer):
    """What the search-and-add list renders as its removable rows."""
    dell = Vendor.objects.get_or_create(
        name="Dell Inc.", defaults={"slug": "dell-inc"})[0]
    board = Component.objects.create(
        vendor=dell, name="0M83RH", kind=ComponentKind.motherboard.value,
        role=ComponentRole.MODEL, slug="dell-0m83rh-assign",
    )
    run = _pending(submitter)
    services.assign_listing(run, system=None, components=[board], level="",
                            by=reviewer)

    form = RunListingAssignForm(
        run=run, initial={"components": run.listing_components.all()}
    )
    assert list(form.initial["components"]) == [board]


def test_the_assignment_form_still_posts_and_validates(client, submitter,
                                                       reviewer):
    """The JS only changes the picking; the POST is unchanged."""
    dell = Vendor.objects.get_or_create(
        name="Dell Inc.", defaults={"slug": "dell-inc"})[0]
    existing = System.objects.create(vendor=dell, name="PowerEdge R660",
                                     slug="dell-poweredge-r660-assign")
    run = _pending(submitter)
    client.force_login(reviewer)

    client.post(reverse("review:run_assign_listing", args=[run.pk]),
                {"system": str(existing.pk), "claimed_validation_level": ""})

    run.refresh_from_db()
    assert run.listing_system == existing


# --- what approval will attach -------------------------------------------------


def test_the_reviewer_sees_the_parts_approval_will_attach(submitter):
    """The ties are made on approval, which left this list empty right up to the
    moment of approving - so the CPU and motherboard about to be attached were
    invisible."""
    run = _pending(submitter)

    preview = services.preview_component_ties(run)
    kinds = {entry["kind"] for entry in preview}

    assert "motherboard" in kinds
    assert "cpu" in kinds


def test_the_preview_says_whether_each_part_already_exists(submitter):
    dell = Vendor.objects.get_or_create(
        name="Dell Inc.", defaults={"slug": "dell-inc"})[0]
    Component.objects.create(
        vendor=dell, name="0M83RH", kind=ComponentKind.motherboard.value,
        role=ComponentRole.MODEL, slug="dell-0m83rh-preview",
    )
    run = _pending(submitter)

    board = next(e for e in services.preview_component_ties(run)
                 if e["kind"] == "motherboard")
    cpu = next(e for e in services.preview_component_ties(run)
               if e["kind"] == "cpu")

    assert board["component"] is not None          # matched the existing entry
    assert cpu["component"] is not None            # the seeded Xeon family
    assert cpu["component"].role == ComponentRole.FAMILY


def test_the_preview_creates_nothing(submitter):
    """It runs on every review page load; it must not write to the catalog."""
    run = _pending(submitter)
    before = Component.objects.count()
    vendors_before = Vendor.objects.count()

    services.preview_component_ties(run)

    assert Component.objects.count() == before
    assert Vendor.objects.count() == vendors_before


def test_the_preview_matches_what_approval_actually_ties(submitter, reviewer):
    """The two share component_tie_targets so they cannot drift; this asserts
    the outcome rather than the arrangement."""
    run = _pending(submitter)
    predicted = {
        (entry["kind"], entry["component"].pk if entry["component"] else None)
        for entry in services.preview_component_ties(run)
    }

    services.approve_run(run, by=reviewer)

    tied = {(c.kind, c.pk) for c in run.listing_components.all()}
    # Every part predicted as already-existing was tied to that same entry.
    for kind, pk in predicted:
        if pk is not None:
            assert (kind, pk) in tied, (kind, pk)
    # And nothing predicted is missing from the ties.
    assert len(tied) >= len(predicted)


def test_a_failing_run_previews_nothing(client, submitter, reviewer):
    """Approving a failed run ties nothing, so promising otherwise would be a
    lie on the page."""
    report = f.make_report(
        run_types=["validate"],
        results=[
            f.validate_result("validate.cpu.functional"),
            f.validate_result("validate.storage.smart", status="fail"),
        ],
    )
    run = ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(f.build_bundle(report)),
        source="api",
    )
    run.listing_proposal = {"vendor_name": "Dell Inc.", "name": "PowerEdge R760"}
    run.save(update_fields=["listing_proposal"])
    release(run)
    client.force_login(reviewer)

    body = client.get(reverse("review:run_detail", args=[run.pk])).content.decode()

    assert "Will be attached on approval" not in body


def test_the_page_lists_them(client, submitter, reviewer):
    run = _pending(submitter)
    client.force_login(reviewer)

    body = client.get(reverse("review:run_detail", args=[run.pk])).content.decode()

    assert "Components this run is evidence for" in body
    assert "0M83RH" in body                        # the board from the report
    assert ">new component<" in body


# --- a GPU nobody validated is not evidence ------------------------------------


def _with_gpus(submitter, gpus):
    inventory = f.default_inventory()
    inventory["summary"]["gpus"] = gpus
    return _pending(submitter, inventory=inventory)


def test_a_driverless_gpu_is_never_cataloged(submitter, reviewer):
    """All the run established is that a PCI device answers on the bus. The
    kernel never initialized it and no test touched it, so a catalog entry and
    an attestation would be claims about untested hardware."""
    run = _with_gpus(submitter, [
        {"pci": "07:00.0", "vendor": "matrox", "model": "MGA G200EH",
         "driver": None, "driver_version": None, "runtime": {}, "vbios": None},
    ])

    assert services.preview_component_ties(run) != []      # board and CPU still
    assert not any(e["kind"] == "gpu" for e in services.preview_component_ties(run))

    services.approve_run(run, by=reviewer)

    assert not Component.objects.filter(name__icontains="G200EH").exists()
    assert not any(c.kind == ComponentKind.gpu.value
                   for c in run.listing_components.all())


def test_a_driverless_gpu_is_not_attached_to_the_system(submitter, reviewer):
    run = _with_gpus(submitter, [
        {"pci": "07:00.0", "vendor": "aspeed", "model": "ASPEED Graphics Family",
         "driver": None, "driver_version": None, "runtime": {}, "vbios": None},
    ])

    services.approve_run(run, by=reviewer)

    run.refresh_from_db()
    related = [c.name for c in run.listing_system.related_components.all()]
    assert not any("ASPEED" in name for name in related)


def test_a_gpu_with_a_driver_is_still_cataloged(submitter, reviewer):
    """The rule is about what was validated, not about GPUs in general."""
    run = _with_gpus(submitter, [
        {"pci": "c1:00.0", "vendor": "amd", "model": "Radeon 780M",
         "driver": "amdgpu", "driver_version": "6.12.0", "runtime": {}, "vbios": None},
    ])

    services.approve_run(run, by=reviewer)

    tied = {c.name for c in run.listing_components.all()}
    assert any("780M" in name or "700M" in name for name in tied), tied


def test_a_mixed_machine_catalogs_only_the_driver_bound_one(submitter, reviewer):
    """A server with a BMC adapter and a real accelerator: only the accelerator
    is evidence."""
    run = _with_gpus(submitter, [
        {"pci": "07:00.0", "vendor": "matrox", "model": "MGA G200EH",
         "driver": None, "driver_version": None, "runtime": {}, "vbios": None},
        {"pci": "c1:00.0", "vendor": "amd", "model": "Radeon 780M",
         "driver": "amdgpu", "driver_version": "6.12.0", "runtime": {}, "vbios": None},
    ])

    gpu_previews = [e for e in services.preview_component_ties(run)
                    if e["kind"] == "gpu"]

    assert len(gpu_previews) == 1
    assert "780M" in gpu_previews[0]["raw_model"]


def test_the_run_still_reports_the_driverless_gpu(submitter):
    """Not cataloging it must not mean hiding it: the inventory and the
    informational GPU test still show it."""
    run = _with_gpus(submitter, [
        {"pci": "07:00.0", "vendor": "matrox", "model": "MGA G200EH",
         "driver": None, "driver_version": None, "runtime": {}, "vbios": None},
    ])

    models = [g["model"] for g in run.inventory["summary"]["gpus"]]
    assert "MGA G200EH" in models


# --- the preview shows the part and the group it rolls up to --------------------


def test_the_preview_reports_the_part_and_its_family_separately(submitter):
    """Showing only "AMD EPYC 7003 Series" hides which processor produced the
    evidence, and showing only the part hides what actually gets certified."""
    run = _pending(submitter)      # reports an Intel Xeon Gold 6430

    cpu = next(e for e in services.preview_component_ties(run)
               if e["kind"] == "cpu")

    assert "6430" in cpu["raw_model"]
    assert cpu["family"] is not None
    assert cpu["family"].role == ComponentRole.FAMILY
    assert "Scalable" in cpu["family"].name
    # The family is what gets attached, because certification is per family.
    assert cpu["component"] == cpu["family"]


def test_a_motherboard_has_no_family_to_roll_up_to(submitter):
    """Only CPUs and GPUs have curated families."""
    run = _pending(submitter)

    board = next(e for e in services.preview_component_ties(run)
                 if e["kind"] == "motherboard")

    assert board["family"] is None
    assert board["raw_model"] == "0M83RH"


def test_a_hand_picked_family_shows_no_phantom_part(submitter):
    """The manual path: no model was detected, so there is no specific part to
    show beside the family."""
    family = Component.objects.get(
        name="Intel Xeon Scalable 4th Generation",
        kind=ComponentKind.cpu.value, role=ComponentRole.FAMILY,
    )
    inventory = f.default_inventory()
    inventory["summary"]["cpus"] = [{"model": "", "vendor": "GenuineIntel"}]
    run = _pending(submitter, inventory=inventory)
    run.listing_proposal = {"vendor_name": "Dell Inc.", "name": "PowerEdge R760",
                            "cpu_family": str(family.pk)}
    run.save(update_fields=["listing_proposal"])

    cpu = next(e for e in services.preview_component_ties(run)
               if e["kind"] == "cpu")

    assert cpu["raw_model"] is None
    assert cpu["family"] == family


def test_the_preview_is_grouped_by_kind_in_a_stable_order(submitter):
    """A flat list makes a reviewer scan for "which of these is the CPU"."""
    run = _pending(submitter)

    groups = services.preview_component_groups(run)
    labels = [g["label"] for g in groups]

    assert labels[0] == "Motherboard"       # what identifies a machine first
    assert "CPU" in labels                  # canonical label, not "Cpu"
    assert all(g["entries"] for g in groups)


def test_the_page_shows_both_the_part_and_the_group(client, submitter, reviewer):
    run = _pending(submitter)
    client.force_login(reviewer)

    body = client.get(reverse("review:run_detail", args=[run.pk])).content.decode()

    assert "Components this run is evidence for" in body
    assert "groups into" in body                       # the family line
    assert "Intel Xeon Scalable 4th Generation" in body
    assert "Xeon(R) Gold 6430" in body or "Gold 6430" in body
    assert "Motherboard" in body and "CPU" in body     # group headers


def test_a_part_the_catalog_lacks_is_marked_as_a_new_component(submitter):
    """Stated outright rather than inferred from the absence of a link."""
    run = _pending(submitter)      # nothing in the catalog matches its board

    board = next(e for e in services.preview_component_ties(run)
                 if e["kind"] == "motherboard")

    assert board["will_create"] is True
    assert board["component"] is None


def test_an_existing_part_is_not_marked_new(submitter):
    dell = Vendor.objects.get_or_create(
        name="Dell Inc.", defaults={"slug": "dell-inc"})[0]
    Component.objects.create(
        vendor=dell, name="0M83RH", kind=ComponentKind.motherboard.value,
        role=ComponentRole.MODEL, slug="dell-0m83rh-new-check",
    )
    run = _pending(submitter)

    board = next(e for e in services.preview_component_ties(run)
                 if e["kind"] == "motherboard")

    assert board["will_create"] is False
    assert board["new_vendor"] is False


def test_a_part_whose_vendor_is_unknown_flags_the_new_vendor_too(submitter):
    """_vendor_for mints a manufacturer when nothing resolves, which is how a
    vendor called "OEM" would have reached the catalog. The reviewer has to be
    able to catch it before approval, not after."""
    inventory = f.default_inventory()
    inventory["summary"]["baseboard"] = {"vendor": "Nobody Has Heard Of This",
                                         "product": "BRD-1"}
    run = _pending(submitter, inventory=inventory)

    board = next(e for e in services.preview_component_ties(run)
                 if e["kind"] == "motherboard")

    assert board["will_create"] is True
    assert board["new_vendor"] is True
    assert board["brand"] == "Nobody Has Heard Of This"


def test_a_resolved_vendor_is_not_flagged_new(submitter):
    """"Dell" resolving to "Dell Inc." through an alias is not a new vendor."""
    run = _pending(submitter)
    board = next(e for e in services.preview_component_ties(run)
                 if e["kind"] == "motherboard")
    assert board["new_vendor"] is False
    assert board["brand"] == "Dell Inc."


def test_the_page_says_new_component_and_names_the_new_vendor(client, submitter,
                                                             reviewer):
    inventory = f.default_inventory()
    inventory["summary"]["baseboard"] = {"vendor": "Nobody Has Heard Of This",
                                         "product": "BRD-1"}
    run = _pending(submitter, inventory=inventory)
    client.force_login(reviewer)

    body = client.get(reverse("review:run_detail", args=[run.pk])).content.decode()

    assert "new component" in body
    assert "and a new vendor" in body
    assert "Nobody Has Heard Of This" in body


# --- the run page's linked-components list --------------------------------------


def test_linked_components_are_listed_one_per_line_with_their_type(
        client, submitter, reviewer):
    """A comma-separated run of names gives no clue which is the board and which
    is the CPU, and the names themselves often do not say."""
    run = _pending(submitter)
    services.approve_run(run, by=reviewer)
    client.force_login(submitter)

    body = client.get(run.get_absolute_url()).content.decode()

    assert "Linked components" in body
    # Each row carries the component kind as its own label.
    assert "Motherboard" in body
    assert "CPU" in body
    # And no comma-joined list.
    assert "</a>," not in body


def test_the_list_is_ordered_by_kind(submitter, reviewer):
    run = _pending(submitter)
    services.approve_run(run, by=reviewer)

    kinds = list(
        run.listing_components.order_by("kind", "name").values_list("kind", flat=True)
    )
    assert kinds == sorted(kinds)


def test_a_family_name_does_not_repeat_its_vendor(submitter):
    """The curated families are named the way the vendor writes the product, so
    prefixing the vendor again reads "AMD AMD Ryzen 7000 Series"."""
    family = Component.objects.filter(
        role=ComponentRole.FAMILY, name__startswith="AMD"
    ).first()
    assert family is not None
    assert family.vendor.name == "AMD"

    assert family.display_label == family.name
    assert not family.display_label.startswith("AMD AMD")
    # __str__ is deliberately untouched: it is what the admin and audit log show.
    assert str(family).startswith("AMD AMD")


def test_a_model_without_a_vendor_prefix_still_gets_one(submitter):
    """"0M83RH" says nothing on its own."""
    dell = Vendor.objects.get_or_create(
        name="Dell Inc.", defaults={"slug": "dell-inc"})[0]
    board = Component.objects.create(
        vendor=dell, name="0M83RH", kind=ComponentKind.motherboard.value,
        role=ComponentRole.MODEL, slug="dell-0m83rh-label",
    )
    assert board.display_label == "Dell Inc. 0M83RH"
