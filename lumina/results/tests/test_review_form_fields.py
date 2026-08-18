"""The submission review form: comboboxes, taxonomy, releases, private notes.

Each free-text identity field is backed by the values already in the catalog so
a submitter reuses "Dell Inc." instead of adding "Dell", while still being able
to type hardware nobody has submitted before.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import Group, User
from django.urls import reverse

from lumina.hardware.models import (
    Component,
    ComponentKind,
    ComponentRole,
    ListingCategoryValue,
    ListingVersion,
    System,
)
from lumina.releases.models import AlmaLinuxRelease
from lumina.results import ingest, services
from lumina.results.forms import ComboBoxInput, RunListingProposalForm
from lumina.results.tests import factories as f
from lumina.taxonomy.models import Category, CategoryValue
from lumina.vendors.models import Vendor

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def releases():
    """AlmaLinux releases are seeded by the devstack command, not a migration,
    so a test database has none until something creates them."""
    for major in (8, 9, 10):
        AlmaLinuxRelease.objects.get_or_create(major=major,
                                               defaults={"supported": True})


@pytest.fixture
def submitter():
    return User.objects.create_user("combo-sub", email="cs@example.com")


@pytest.fixture
def reviewer():
    user = User.objects.create_user("combo-rev", email="cr@example.com")
    group, _ = Group.objects.get_or_create(name="reviewer")
    user.groups.add(group)
    return user


def _run(submitter, **report_kw):
    report = f.make_report(
        run_types=["validate"],
        results=[f.validate_result("validate.cpu.functional")],
        **report_kw,
    )
    return ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(f.build_bundle(report)),
        source="api",
    )


def _category(name, slug, values, widget="checkboxes", suggestions=True):
    category = Category.objects.get_or_create(
        slug=slug,
        defaults={"name": name, "picker_widget": widget,
                  "allow_suggestions": suggestions},
    )[0]
    for value in values:
        CategoryValue.objects.get_or_create(
            category=category, value=value,
            defaults={"status": CategoryValue.STATUS_APPROVED},
        )
    return category


# --- comboboxes ----------------------------------------------------------------


def test_identity_fields_are_comboboxes(submitter):
    run = _run(submitter)
    form = RunListingProposalForm(run=run, user=submitter)
    # cpu_model is no longer here: a detected CPU is corrected in the components section.
    for field in ("vendor_name", "name", "model_number"):
        assert isinstance(form.fields[field].widget, ComboBoxInput), field


def test_the_vendor_combobox_offers_existing_vendors(submitter):
    Vendor.objects.get_or_create(name="Dell Inc.", defaults={"slug": "dell-inc"})
    run = _run(submitter)
    form = RunListingProposalForm(run=run, user=submitter)
    assert "Dell Inc." in form.fields["vendor_name"].widget.options


def test_free_text_is_still_accepted(submitter):
    """Hardware nobody has submitted has to be typeable - that is the point of
    the field. The list is a nudge, not a constraint."""
    run = _run(submitter)
    form = RunListingProposalForm(
        data={"vendor_name": "A Vendor Nobody Has Listed", "name": "Model X"},
        run=run, user=submitter,
    )
    assert form.is_valid(), form.errors
    assert form.cleaned_data["vendor_name"] == "A Vendor Nobody Has Listed"


def test_the_cpu_combobox_lists_models_not_families(submitter):
    """This field records the exact part; a family here would log a family as
    though it were a processor. It only appears when nothing was detected - a detected CPU is
    corrected in the components section instead - so this uses a run with no reported model."""
    intel = Vendor.objects.get_or_create(name="Intel", defaults={"slug": "intel"})[0]
    Component.objects.create(
        vendor=intel, name="Xeon Gold 6430", kind=ComponentKind.cpu.value,
        role=ComponentRole.MODEL, slug="intel-xeon-gold-6430-combo",
    )
    inventory = f.default_inventory()
    inventory["summary"]["cpus"] = [{"model": "", "vendor": "GenuineIntel"}]
    run = _run(submitter, inventory=inventory)
    options = RunListingProposalForm(run=run, user=submitter).fields[
        "cpu_model"].widget.options

    assert "Xeon Gold 6430" in options
    assert not any("Series" in option or "Generation" in option for option in options)


def test_the_name_combobox_follows_the_subject(submitter):
    """A custom build is listed as a motherboard, so offering System names
    would invite naming a board after a server."""
    dell = Vendor.objects.get_or_create(
        name="Dell Inc.", defaults={"slug": "d", "verified": True},
    )[0]
    System.objects.create(vendor=dell, name="PowerEdge R760", slug="pe-r760-combo")
    Component.objects.create(
        vendor=dell, name="0M83RH", kind=ComponentKind.motherboard.value,
        role=ComponentRole.MODEL, slug="dell-0m83rh-combo",
    )
    # Creating that System makes the run auto-link to it, and the identity fields are hidden
    # from anyone who does not speak for the listing's vendor. This test is about which names
    # the combobox offers, not about who may see it, so the submitter speaks for Dell.
    from lumina.vendors.models import VendorMembership

    VendorMembership.objects.create(
        user=submitter, vendor=dell, role=VendorMembership.ROLE_SUBMITTER,
    )
    run = _run(submitter)

    as_system = RunListingProposalForm(run=run, user=submitter, subject="system")
    as_board = RunListingProposalForm(run=run, user=submitter, subject="motherboard")

    assert "PowerEdge R760" in as_system.fields["name"].widget.options
    assert "0M83RH" in as_board.fields["name"].widget.options
    assert "PowerEdge R760" not in as_board.fields["name"].widget.options


def test_the_widget_renders_a_datalist_so_it_works_without_javascript():
    widget = ComboBoxInput(["Dell Inc.", "Supermicro"])
    html = widget.render("vendor_name", "")

    assert 'list="combo-vendor_name"' in html
    assert '<datalist id="combo-vendor_name">' in html
    assert '<option value="Dell Inc."></option>' in html
    assert 'data-combobox="true"' in html


def test_combobox_options_are_escaped():
    html = ComboBoxInput(['Ac"me <script>']).render("vendor_name", "")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


# --- private notes -------------------------------------------------------------


def test_submitter_notes_are_offered_and_marked_private(client, submitter):
    run = _run(submitter)
    client.force_login(submitter)

    body = client.get(reverse("results:propose_listing", args=[run.uuid])).content.decode()

    assert "Notes for the reviewer" in body
    assert "Never published" in body


def test_submitter_notes_land_on_the_run_not_the_listing(client, submitter):
    run = _run(submitter)
    client.force_login(submitter)

    client.post(reverse("results:propose_listing", args=[run.uuid]), {
        "vendor_name": "Dell Inc.", "name": "PowerEdge R760",
        "submitter_notes": "BIOS 2.4.4, SR-IOV disabled.",
    })

    run.refresh_from_db()
    assert run.submitter_notes == "BIOS 2.4.4, SR-IOV disabled."
    assert "submitter_notes" not in run.listing_proposal


# --- taxonomy ------------------------------------------------------------------


def test_categories_appear_as_fields(submitter):
    _category("Form factor", "form-factor", ["1U", "2U", "Tower"])
    run = _run(submitter)

    form = RunListingProposalForm(run=run, user=submitter)

    assert "cat_form-factor" in form.fields
    assert "propose_form-factor" in form.fields       # allow_suggestions=True


def test_a_curated_category_offers_no_proposal_box(submitter):
    _category("Form factor", "form-factor", ["1U"], widget="dropdown",
              suggestions=False)
    run = _run(submitter)

    form = RunListingProposalForm(run=run, user=submitter)

    assert "cat_form-factor" in form.fields
    assert "propose_form-factor" not in form.fields


def test_a_derived_category_is_not_offered(submitter):
    """Architecture is set from the run's own kernel report at approval, so asking
    the submitter would invite an answer that contradicts the machine.

    Both submit paths exclude it - see
    ``hardware/tests/test_derived_architecture.py`` for the other one and for the
    binding itself.
    """
    category = _category("Architecture", "architecture", ["x86_64", "aarch64"],
                         widget="dropdown", suggestions=False)
    category.derived_from_runs = True
    category.save(update_fields=["derived_from_runs"])
    run = _run(submitter)

    form = RunListingProposalForm(run=run, user=submitter)

    assert "cat_architecture" not in form.fields
    assert "propose_architecture" not in form.fields


def test_chosen_categories_bind_to_the_created_listing(submitter, reviewer):
    _category("Form factor", "form-factor", ["1U", "2U"])
    nvme = CategoryValue.objects.get(category__slug="form-factor", value="2U")
    run = _run(submitter)
    run.listing_proposal = {"vendor_name": "Dell Inc.", "name": "PowerEdge R760",
                            "cat_form-factor": [nvme.slug]}
    run.save(update_fields=["listing_proposal"])

    services.create_listings_from_run(run, by=reviewer)

    system = System.objects.get(name="PowerEdge R760")
    assert ListingCategoryValue.objects.filter(
        listing_system=system, value=nvme
    ).exists()


def test_custom_build_categories_bind_to_its_system(submitter, reviewer):
    """A custom build is a System now, so its taxonomy tags belong on that System (browseable and
    filterable), not on the shared motherboard part."""
    _category("Form factor", "form-factor", ["1U", "2U"])
    two_u = CategoryValue.objects.get(category__slug="form-factor", value="2U")
    run = _run(submitter, inventory=f.custom_build_inventory())
    run.listing_proposal = {"vendor_name": "ASRock", "name": "B650M PG Riptide",
                            "machine_kind": "custom", "cat_form-factor": [two_u.slug]}
    run.save(update_fields=["listing_proposal"])

    services.create_listings_from_run(run, by=reviewer)

    system = System.objects.get(name="B650M PG Riptide")
    board = Component.objects.get(name="B650M PG Riptide",
                                 kind=ComponentKind.motherboard.value)
    assert ListingCategoryValue.objects.filter(listing_system=system, value=two_u).exists()
    assert not ListingCategoryValue.objects.filter(
        listing_component=board, value=two_u
    ).exists()


def test_a_custom_build_is_offered_the_system_category_axes(submitter):
    """Its listing is a System, so a system-only axis appears on the propose form - it would not
    have when a custom build was catalogued as a component."""
    category = _category("Form factor", "form-factor", ["1U", "2U"])
    category.applies_to = Category.APPLIES_SYSTEM
    category.save(update_fields=["applies_to"])
    run = _run(submitter, inventory=f.custom_build_inventory())

    form = RunListingProposalForm(run=run, user=submitter, subject="motherboard")

    assert "cat_form-factor" in form.fields


def test_a_proposed_value_is_pending_not_a_live_filter_option(submitter, reviewer):
    """Otherwise a submitter could mint filter options by typing them."""
    _category("Storage", "storage", ["SATA"])
    run = _run(submitter)
    run.listing_proposal = {"vendor_name": "Dell Inc.", "name": "PowerEdge R760",
                            "propose_storage": "CXL"}
    run.save(update_fields=["listing_proposal"])

    services.create_listings_from_run(run, by=reviewer)

    proposed = CategoryValue.objects.get(value="CXL")
    assert proposed.status == CategoryValue.STATUS_PENDING
    assert proposed.proposed_by == submitter
    system = System.objects.get(name="PowerEdge R760")
    assert not ListingCategoryValue.objects.filter(
        listing_system=system, value=proposed
    ).exists()


# --- AlmaLinux releases --------------------------------------------------------


def test_a_checkbox_per_supported_release(submitter):
    """One box per major, and nothing beside it.

    A minimum-minor dropdown used to sit next to each. Hardware certifies per major now, so
    the whole per-major-cap machinery went with it - the ``max_minor`` field an admin could
    raise, the run's-own-minor exemption for a cap nobody had raised yet, and the coercion of
    the posted string to an integer. Four tests here covered that and are gone.
    """
    run = _run(submitter)
    form = RunListingProposalForm(run=run, user=submitter)

    for release in AlmaLinuxRelease.objects.supported():
        assert f"release_{release.major}" in form.fields
        assert f"release_minor_{release.major}" not in form.fields


def test_the_run_s_own_release_is_prefilled(submitter):
    """The major the run passed on is ticked. Which minor it was is on the run's own record."""
    run = _run(submitter)          # the factory reports 9.6

    initial = RunListingProposalForm.initial_from_run(run)

    assert initial[f"release_{run.alma_release.major}"] is True
    assert not any(key.startswith("release_minor") for key in initial)


def test_declared_releases_are_recorded_on_the_listing(submitter, reviewer):
    """A vendor stating their machine also supports 8 when the run was on 9."""
    eight = AlmaLinuxRelease.objects.get(major=8)
    run = _run(submitter)
    run.listing_proposal = {"vendor_name": "Dell Inc.", "name": "PowerEdge R760",
                            "release_8": True}
    run.save(update_fields=["listing_proposal"])

    services.create_listings_from_run(run, by=reviewer)

    system = System.objects.get(name="PowerEdge R760")
    version = ListingVersion.objects.get(listing_system=system, release=eight)
    assert version.source == ListingVersion.SOURCE_DECLARED


def test_an_unticked_release_records_nothing(submitter, reviewer):
    run = _run(submitter)
    run.listing_proposal = {"vendor_name": "Dell Inc.", "name": "PowerEdge R760",
                            "release_8": False}
    run.save(update_fields=["listing_proposal"])

    services.create_listings_from_run(run, by=reviewer)

    system = System.objects.get(name="PowerEdge R760")
    assert not ListingVersion.objects.filter(
        listing_system=system, release__major=8
    ).exists()


# --- checkbox group markup -----------------------------------------------------


def test_checkbox_groups_render_bootstrap_rows_not_a_bare_list(client, submitter):
    """Django's default <ul><li><label><input> markup plus Bootstrap's
    .form-check-input (which expects a .form-check wrapper) made the labels
    overlap each other and the field below."""
    _category("Form factor", "form-factor", ["1U", "2U", "Tower"])
    run = _run(submitter)
    client.force_login(submitter)

    body = client.get(reverse("results:propose_listing", args=[run.uuid])).content.decode()

    assert '<ul id="id_cat_form-factor"' not in body
    assert 'class="form-check mb-0"' in body
    assert 'class="form-check-label"' in body


def test_the_form_is_grouped_into_sections(client, submitter):
    run = _run(submitter)
    client.force_login(submitter)

    body = client.get(reverse("results:propose_listing", args=[run.uuid])).content.decode()

    # No "Processor" section: the CPU was detected, so it is corrected in the components section.
    for heading in ("Identity", "AlmaLinux compatibility", "Notes for the reviewer"):
        assert heading in body, heading
    assert "Processor" not in body


# --- correcting the detected machine kind ---------------------------------------


def test_the_machine_kind_is_always_offered(submitter):
    """The detected kind is a heuristic over firmware strings and was wrong on
    a real Lenovo laptop, so whoever is holding the machine has to be able to
    overrule it - not only when nothing was detected."""
    run = _run(submitter)
    assert run.system_kind == "prebuilt"

    form = RunListingProposalForm(run=run, user=submitter)

    assert "machine_kind" in form.fields
    assert form.fields["machine_kind"].required is False


def test_the_detected_kind_is_prefilled(submitter):
    run = _run(submitter)
    assert RunListingProposalForm.initial_from_run(run)["machine_kind"] == "prebuilt"


def test_an_unidentified_machine_guesses_no_manufacturer(submitter):
    """A guess nobody made must not look like an answer - and the vendor is the guess that would
    matter, because it is what mints a catalog manufacturer."""
    inventory = f.default_inventory()
    inventory["summary"]["system"] = {"vendor": None, "product": None, "bios": {}}
    inventory["summary"]["baseboard"] = {"vendor": None, "product": "7D2XCTO1WW"}
    run = _run(submitter, inventory=inventory)

    # Answered "custom", which is the fallback rather than a guess: nothing here claims to be a
    # vendor-built product. It used to prefill blank, because a third kind let the form say "we do
    # not know".
    #
    # The manufacturer is still not guessed - that is the part that matters, and it is why the
    # radio being pre-answered is safe: the submitter cannot accept this form without typing a
    # vendor. The board *model* does prefill, because the firmware really did report one.
    initial = RunListingProposalForm.initial_from_run(run)
    assert initial["machine_kind"] == "custom"
    assert initial["vendor_name"] in ("", None)
    assert initial["name"] == "7D2XCTO1WW"
    form = RunListingProposalForm(run=run, user=submitter, subject="machine")
    assert form.fields["machine_kind"].required is True


def test_a_corrected_kind_lists_the_board_and_a_system_for_it(submitter, reviewer):
    """Detected prebuilt, submitter says custom: the catalog lists the board, and a System
    identified by that board so the build appears under Systems - but not a System named after the
    machine's own DMI identity, which detection guessed and the correction overrode."""
    run = _run(submitter)                      # detected prebuilt
    run.listing_proposal = {"vendor_name": "ASRock", "name": "B650M PG Riptide",
                            "machine_kind": "custom"}
    run.save(update_fields=["listing_proposal"])

    services.create_listings_from_run(run, by=reviewer)

    run.refresh_from_db()
    board = Component.objects.get(name="B650M PG Riptide")
    assert board.kind == ComponentKind.motherboard.value
    assert board in run.listing_components.all()
    # Exactly one System, and it is the board's - not the detected (wrong) prebuilt identity.
    assert list(System.objects.values_list("name", flat=True)) == ["B650M PG Riptide"]
    system = System.objects.get(name="B650M PG Riptide")
    assert system.vendor.name == "ASRock"
    assert run.listing_system == system


def test_the_reverse_correction_also_works(submitter, reviewer):
    """Detected custom, submitter says it is a vendor system."""
    run = _run(submitter, inventory=f.custom_build_inventory())
    assert run.system_kind == "custom"
    run.listing_proposal = {"vendor_name": "Lenovo", "name": "ThinkSystem SR645",
                            "machine_kind": "prebuilt"}
    run.save(update_fields=["listing_proposal"])

    services.create_listings_from_run(run, by=reviewer)

    system = System.objects.get(name="ThinkSystem SR645")
    assert run.listing_system == system


def test_no_answer_keeps_the_detected_kind(submitter, reviewer):
    run = _run(submitter)
    run.listing_proposal = {"vendor_name": "Dell Inc.", "name": "PowerEdge R760"}
    run.save(update_fields=["listing_proposal"])

    services.create_listings_from_run(run, by=reviewer)

    assert run.listing_system == System.objects.get(name="PowerEdge R760")


def test_only_the_tested_major_is_ticked(submitter):
    """A 9.6 run says nothing about 8 or 10, so it ticks neither.

    This used to guard something narrower: the minor was inherited across majors, producing
    an "8.6 and later" claim nobody made out of a number with no relationship to that release.
    The floor is gone; the rule that a run speaks only for its own major is not.
    """
    run = _run(submitter)                      # factory reports 9.6

    initial = RunListingProposalForm.initial_from_run(run)

    assert initial["release_9"] is True
    assert "release_8" not in initial and "release_10" not in initial


def test_the_tick_renders_as_checked(client, submitter):
    """The prefill has to survive into the markup, not just the dict."""
    import re

    run = _run(submitter)
    client.force_login(submitter)

    body = client.get(reverse("results:propose_listing", args=[run.uuid])).content.decode()

    assert re.search(r'name="release_9"[^>]*checked', body)
    assert 'name="release_minor_9"' not in body


# --- the corrected identity has to appear everywhere ---------------------------


def test_the_display_name_uses_the_submitters_correction(submitter):
    """Run 71314765: detected as "Custom build: OEM 7D2XCTO1WW". The submitter
    corrected it to a Lenovo system, the review detail page showed the
    correction, and every list kept showing the detected name - the same run
    under two names depending on the page."""
    inventory = f.default_inventory()
    # A real board maker, because the kind is derived now: an unbranded "OEM" board with no
    # system product reads as *unknown*, not custom - the firmware names nobody, so the machine
    # could be anything. The fixture used to declare "custom" and contradict its own inputs.
    inventory["summary"]["system"] = {"vendor": None, "product": None, "bios": {}}
    inventory["summary"]["baseboard"] = {"vendor": "ASRock", "product": "B650M PG Riptide"}
    run = _run(submitter, inventory=inventory)
    assert run.display_name == "Custom build: ASRock B650M PG Riptide"

    run.listing_proposal = {"vendor_name": "Lenovo", "name": "ThinkSystem SR645",
                            "machine_kind": "prebuilt"}
    run.save(update_fields=["listing_proposal"])

    assert run.display_name == "Lenovo ThinkSystem SR645"
    assert "Custom build" not in run.display_name


def test_a_correction_to_custom_is_reflected_too(submitter):
    run = _run(submitter)                      # detected prebuilt Dell
    run.listing_proposal = {"vendor_name": "ASRock", "name": "B650M PG Riptide",
                            "machine_kind": "custom"}
    run.save(update_fields=["listing_proposal"])

    assert run.display_name == "Custom build: ASRock B650M PG Riptide"


def test_the_detected_name_stands_when_nothing_was_corrected(submitter):
    run = _run(submitter)
    assert run.display_name == "Dell Inc. PowerEdge R760"


def test_the_corrected_name_shows_in_the_submitters_run_list(client, submitter):
    """On the dashboard, which is where a submitter's runs are listed now."""
    run = _run(submitter)
    run.listing_proposal = {"vendor_name": "ASRock", "name": "B650M PG Riptide",
                            "machine_kind": "custom"}
    run.save(update_fields=["listing_proposal"])
    client.force_login(submitter)

    body = client.get(reverse("accounts:dashboard")).content.decode()

    assert "Custom build: ASRock B650M PG Riptide" in body
    assert "PowerEdge R760" not in body


def test_the_corrected_name_shows_in_the_review_queue(client, submitter, reviewer):
    run = _run(submitter)
    run.listing_proposal = {"vendor_name": "Lenovo", "name": "ThinkSystem SR645",
                            "machine_kind": "prebuilt"}
    run.status = run.STATUS_PENDING
    run.save(update_fields=["listing_proposal", "status"])
    client.force_login(reviewer)

    body = client.get(reverse("review:queue")).content.decode()

    assert "Lenovo ThinkSystem SR645" in body


# --- a correction has to show everywhere, not just in the title ----------------


def _detected_custom(submitter):
    inventory = f.default_inventory()
    # A real board maker, because the kind is derived now: an unbranded "OEM" board with no
    # system product reads as *unknown*, not custom - the firmware names nobody, so the machine
    # could be anything. The fixture used to declare "custom" and contradict its own inputs.
    inventory["summary"]["system"] = {"vendor": None, "product": None, "bios": {}}
    inventory["summary"]["baseboard"] = {"vendor": "ASRock", "product": "B650M PG Riptide"}
    return _run(submitter, inventory=inventory)


def test_correcting_the_kind_changes_what_the_summary_says(client, submitter):
    """The Summary block read the raw detected kind, so a submitter who had
    corrected it to prebuilt still saw a "Custom build" badge - which reads as
    the correction not having stuck."""
    run = _detected_custom(submitter)
    client.force_login(submitter)

    body = client.get(run.get_absolute_url()).content.decode()
    assert "Custom build" in body

    run.listing_proposal = {"vendor_name": "Lenovo", "name": "ThinkSystem SR645",
                            "machine_kind": "prebuilt"}
    run.save(update_fields=["listing_proposal"])

    body = client.get(run.get_absolute_url()).content.decode()
    assert "Custom build" not in body
    assert "Lenovo" in body and "ThinkSystem SR645" in body


def test_the_kind_and_identity_come_from_one_rule(submitter):
    """display_name, the summary, the subject, and listing creation all used to
    re-derive this; a divergence is what produced two names for one run."""
    run = _detected_custom(submitter)
    assert run.effective_system_kind == "custom"
    assert run.effective_vendor == "ASRock"

    run.listing_proposal = {"vendor_name": "Lenovo", "name": "ThinkSystem SR645",
                            "machine_kind": "prebuilt"}
    run.save(update_fields=["listing_proposal"])

    assert run.effective_system_kind == "prebuilt"
    assert run.effective_vendor == "Lenovo"
    assert run.effective_product == "ThinkSystem SR645"
    assert run.display_name == "Lenovo ThinkSystem SR645"


def test_the_raw_detection_is_still_kept(submitter):
    """system_kind is evidence - what the firmware said - and must not be
    overwritten by the correction."""
    run = _detected_custom(submitter)
    run.listing_proposal = {"machine_kind": "prebuilt"}
    run.save(update_fields=["listing_proposal"])
    run.refresh_from_db()

    assert run.system_kind == "custom"
    assert run.effective_system_kind == "prebuilt"


def test_an_uncorrected_run_is_unchanged(submitter):
    run = _run(submitter)
    assert run.effective_system_kind == run.system_kind
    assert run.effective_vendor == run.system_vendor
    assert run.effective_product == run.system_product
