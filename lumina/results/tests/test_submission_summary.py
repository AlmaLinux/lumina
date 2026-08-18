"""Telling a submitter what their submission will actually do.

Three things reported together, all versions of the same complaint: the form asked for
answers and said nothing about the consequences.

1. Which typed values match an existing catalog entry and which create a new one.
2. The components the run is evidence for, matched or new, with a way to drop one.
3. A summary of the net effect, including before/after on text and what approval adds to
   each AlmaLinux release.

The removal part turned out to be a gap rather than a port of something reviewers had.
Reviewers appear to be able to clear a run's component list through
``review:run_assign_listing``, but ``ensure_component_ties`` re-derives every tie from the
report at approval, so it all came back: measured at 0 components after clearing and 3
again after approving. ``TestRun.excluded_component_ties`` is what makes a removal stick,
for reviewers as much as submitters.

The summary's own logic lives in ``static/js/submission-summary.js`` and is exercised by
``tests/js/summary_check.js`` - the wording of a consequence is the part worth testing, and
getting one backwards is how a summary lies. This file covers the server half: the payload
it reads, and the fact that the page carries it.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth.models import Group, User
from django.urls import reverse

from lumina.hardware.models import Component, ComponentKind, ListingVersion, System
from lumina.releases.models import AlmaLinuxRelease
from lumina.results import ingest, services
from lumina.results.models import TestRun
from lumina.results.tests import factories as f
from lumina.results.tests.helpers import release
from lumina.vendors.models import Vendor, VendorMembership

pytestmark = pytest.mark.django_db


@pytest.fixture
def submitter():
    return User.objects.create_user("sum-sub", email="s@example.com")


@pytest.fixture
def reviewer():
    user = User.objects.create_user("sum-rev")
    group, _ = Group.objects.get_or_create(name="reviewer")
    user.groups.add(group)
    return user


def _run(submitter, version_id="9.6"):
    return ingest.ingest_bundle(
        submitter=submitter, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"], version_id=version_id,
            results=[f.validate_result("validate.cpu.functional")],
        ))),
    )


def _dell_listing(submitter, *, member=False):
    dell = Vendor.objects.create(name="Dell Inc.", verified=True)
    listing = System.objects.create(
        vendor=dell, name="PowerEdge R760", owner_vendor=dell,
        model_number="R760", description="Existing text.",
    )
    if member:
        VendorMembership.objects.create(
            user=submitter, vendor=dell, role=VendorMembership.ROLE_SUBMITTER,
        )
    return listing


# --- the payload ----------------------------------------------------------------


def test_a_new_listing_has_no_before(submitter):
    """``listing: null`` is what tells the summary to say "creates" rather than diff."""
    run = _run(submitter)

    payload = services.submission_preview(run, submitter)

    assert payload["listing"] is None


def test_an_existing_listing_supplies_its_current_values(submitter):
    listing = _dell_listing(submitter)
    run = _run(submitter)

    payload = services.submission_preview(run, submitter)

    assert payload["listing"]["name"] == "PowerEdge R760"
    assert payload["listing"]["model_number"] == "R760"
    assert payload["listing"]["description"] == "Existing text."
    assert payload["listing"]["label"] == str(listing)


def test_known_names_are_included_for_the_match_badges(submitter):
    _dell_listing(submitter)
    run = _run(submitter)

    payload = services.submission_preview(run, submitter)

    assert "Dell Inc." in payload["known"]["vendor"]
    assert "PowerEdge R760" in payload["known"]["system"]


def test_existing_releases_carry_their_floor(submitter):
    listing = _dell_listing(submitter)
    nine, _ = AlmaLinuxRelease.objects.get_or_create(
        major=9, defaults={"supported": True},
    )
    ListingVersion.objects.create(
        listing_system=listing, release=nine,
        source=ListingVersion.SOURCE_RUN,
    )
    run = _run(submitter)

    payload = services.submission_preview(run, submitter)

    assert payload["versions"]["9"]["source"] == "run"
    assert payload["versions"]["9"]["mine"] is False


def test_a_release_this_person_already_attested_says_so(submitter, reviewer):
    """The difference between "adds your confirmation" and "no change". Attestations are
    one per (version, person), so a second run of theirs adds evidence but not a second
    confirmation, and a summary promising one would inflate the count in the reader's
    head."""
    _dell_listing(submitter)
    first = _run(submitter)
    services.approve_run(release(first), by=reviewer)

    second = _run(submitter, version_id="9.6")
    payload = services.submission_preview(second, submitter)

    assert payload["versions"]["9"]["mine"] is True


def test_another_persons_attestation_is_not_mine(submitter, reviewer):
    _dell_listing(submitter)
    other = User.objects.create_user("someone-else")
    services.approve_run(release(_run(other)), by=reviewer)

    payload = services.submission_preview(_run(submitter), submitter)

    assert payload["versions"]["9"]["attestations"] == 1
    assert payload["versions"]["9"]["mine"] is False


def test_components_report_matched_and_new(submitter):
    run = _run(submitter)

    kinds = {c["kind"]: c for c in services.submission_preview(run, submitter)["components"]}

    # The fixture reports a board, a Xeon, and an L40S. The Xeon rolls up to a seeded
    # family; the other two are not in the catalog.
    assert kinds["cpu"]["will_create"] is False
    assert kinds["cpu"]["matches"]
    assert kinds["gpu"]["will_create"] is True
    assert kinds["gpu"]["matches"] == ""


def test_every_component_carries_a_key_and_a_label(submitter):
    run = _run(submitter)

    for entry in services.submission_preview(run, submitter)["components"]:
        assert entry["key"], entry
        assert entry["label"].strip(), entry


def test_the_payload_is_json_serializable(submitter):
    """It goes through ``json_script``, so a stray model instance is a 500 on the page
    rather than something a test of the dict shape would notice."""
    _dell_listing(submitter)
    run = _run(submitter)

    json.dumps(services.submission_preview(run, submitter))


# --- the page -------------------------------------------------------------------


def test_the_page_carries_the_baseline(client, submitter):
    run = _run(submitter)
    client.force_login(submitter)

    body = client.get(
        reverse("results:propose_listing", args=[run.uuid])
    ).content.decode()

    assert 'id="submission-baseline"' in body
    assert "submission-summary.js" in body
    assert 'data-summary-body' in body


def test_the_summary_starts_hidden(client, submitter):
    """With no JavaScript there is nothing to show, and an empty card headed "What this
    submission will do" would read as "nothing"."""
    run = _run(submitter)
    client.force_login(submitter)

    body = client.get(
        reverse("results:propose_listing", args=[run.uuid])
    ).content.decode()

    marker = body.index('id="submission-summary"')
    assert "d-none" in body[marker - 120:marker + 40]


def test_the_page_lists_the_components_with_their_status(client, submitter):
    run = _run(submitter)
    client.force_login(submitter)

    raw = client.get(
        reverse("results:propose_listing", args=[run.uuid])
    ).content.decode()
    # Whitespace-normalized: the copy wraps across source lines, so a contiguous-string
    # assertion breaks whenever the template is rewrapped rather than when the behaviour
    # changes.
    body = " ".join(raw.split())

    assert "Components this run is evidence for" in body
    assert "Matches" in body                       # the Xeon family
    assert "approving creates this catalog entry" in body   # the L40S
    assert 'name="included_ties"' in body


# --- removing a component -------------------------------------------------------


def test_a_submitter_can_exclude_a_component(client, submitter):
    """The boxes are an include list, so dropping a part means posting everything except
    it. Checked = keep, which is the way round the copy reads and the way round every
    other checkbox on the page works."""
    run = _run(submitter)
    entries = services.preview_component_ties(run)
    gpu = next(entry for entry in entries if entry["kind"] == "gpu")
    keep = [entry["key"] for entry in entries if entry["key"] != gpu["key"]]
    client.force_login(submitter)

    client.post(reverse("results:propose_listing", args=[run.uuid]), {
        "vendor_name": "Dell Inc.", "name": "PowerEdge R760",
        "machine_kind": "prebuilt", "included_ties": keep,
        "components_submitted": "1",
    })

    run.refresh_from_db()
    assert run.excluded_component_ties == [gpu["key"]]


def test_ticking_every_box_excludes_nothing(client, submitter):
    """The direction, pinned. Reversed at first - checked meant *exclude* - so a submitter
    ticking a component to keep it was telling the form to drop it, and the summary
    dutifully said it would not be attached."""
    run = _run(submitter)
    entries = services.preview_component_ties(run)
    client.force_login(submitter)

    client.post(reverse("results:propose_listing", args=[run.uuid]), {
        "vendor_name": "Dell Inc.", "name": "PowerEdge R760",
        "machine_kind": "prebuilt",
        "included_ties": [entry["key"] for entry in entries],
        "components_submitted": "1",
    })

    run.refresh_from_db()
    assert run.excluded_component_ties == []


def test_every_box_starts_ticked(submitter):
    """Nothing is excluded until somebody says so, so a form nobody has touched keeps
    every component."""
    run = _run(submitter)

    from lumina.results.forms import RunListingProposalForm
    form = RunListingProposalForm(run=run, user=submitter)

    offered = [key for key, _ in form.fields["included_ties"].choices]
    assert offered
    assert form.initial["included_ties"] == offered


def test_the_exclusion_survives_approval(client, submitter, reviewer):
    """The whole point, and the thing that did not work for anybody before.
    ``ensure_component_ties`` re-derives ties from the report, so without a stored
    exclusion the part comes straight back."""
    run = _run(submitter)
    gpu = next(
        entry for entry in services.preview_component_ties(run)
        if entry["kind"] == "gpu"
    )
    run.excluded_component_ties = [gpu["key"]]
    run.save(update_fields=["excluded_component_ties"])

    services.approve_run(release(TestRun.objects.get(pk=run.pk)), by=reviewer)

    run.refresh_from_db()
    tied = {component.kind for component in run.listing_components.all()}
    assert ComponentKind.gpu.value not in tied
    assert ComponentKind.cpu.value in tied, "only the excluded part should be dropped"


def test_an_excluded_component_creates_no_catalog_entry(client, submitter, reviewer):
    run = _run(submitter)
    gpu = next(
        entry for entry in services.preview_component_ties(run)
        if entry["kind"] == "gpu"
    )
    run.excluded_component_ties = [gpu["key"]]
    run.save(update_fields=["excluded_component_ties"])

    services.approve_run(release(TestRun.objects.get(pk=run.pk)), by=reviewer)

    assert not Component.objects.filter(
        kind=ComponentKind.gpu.value, name__icontains="L40S",
    ).exists()


def test_an_excluded_component_stays_listed_and_marked(submitter):
    """Not filtered out of the preview. Dropping it would leave the submitter unable to
    change their mind and a reviewer unable to see the decision was made."""
    run = _run(submitter)
    gpu = next(
        entry for entry in services.preview_component_ties(run)
        if entry["kind"] == "gpu"
    )
    run.excluded_component_ties = [gpu["key"]]
    run.save(update_fields=["excluded_component_ties"])

    entries = {e["kind"]: e for e in services.preview_component_ties(run)}

    assert entries["gpu"]["excluded"] is True
    assert entries["cpu"]["excluded"] is False


def test_the_checkbox_reflects_a_saved_exclusion(client, submitter):
    """An excluded part comes back *unticked*, and its siblings stay ticked."""
    run = _run(submitter)
    entries = services.preview_component_ties(run)
    gpu = next(entry for entry in entries if entry["kind"] == "gpu")
    cpu = next(entry for entry in entries if entry["kind"] == "cpu")
    run.excluded_component_ties = [gpu["key"]]
    run.save(update_fields=["excluded_component_ties"])
    client.force_login(submitter)

    body = client.get(
        reverse("results:propose_listing", args=[run.uuid])
    ).content.decode()

    def box(key):
        start = body.index(f'value="{key}"')
        return body[start:body.index(">", start)]

    assert "checked" not in box(gpu["key"])
    assert "checked" in box(cpu["key"])


def test_a_tie_key_is_stable_across_case_and_spacing(submitter):
    """Keyed on the reported model rather than on position, so an exclusion stays pinned to
    the part it was about even if the GPU order changes between runs."""
    assert services.tie_key(ComponentKind.gpu, "NVIDIA  L40S") == services.tie_key(
        ComponentKind.gpu, "nvidia l40s",
    )
    assert services.tie_key(ComponentKind.gpu, "L40S") != services.tie_key(
        ComponentKind.cpu, "L40S",
    )


def test_re_ticking_a_box_re_includes_the_component(client, submitter):
    """A decision has to be reversible while the run is still the submitter's."""
    run = _run(submitter)
    entries = services.preview_component_ties(run)
    gpu = next(entry for entry in entries if entry["kind"] == "gpu")
    run.excluded_component_ties = [gpu["key"]]
    run.save(update_fields=["excluded_component_ties"])
    client.force_login(submitter)

    client.post(reverse("results:propose_listing", args=[run.uuid]), {
        "vendor_name": "Dell Inc.", "name": "PowerEdge R760",
        "machine_kind": "prebuilt",
        "included_ties": [entry["key"] for entry in entries],
        "components_submitted": "1",
    })

    run.refresh_from_db()
    assert run.excluded_component_ties == []


def test_unticking_everything_excludes_everything(client, submitter):
    """An empty include list is a real answer. A submitter who says none of these parts are
    part of what they are certifying has to be believed.

    What makes it an answer is the hidden marker: the browser posts that even with every box
    unticked, so "all boxes off" and "this section was never on the page" stop looking
    identical. See ``test_a_post_without_the_marker_leaves_the_components_alone``.
    """
    run = _run(submitter)
    client.force_login(submitter)

    client.post(reverse("results:propose_listing", args=[run.uuid]), {
        "vendor_name": "Dell Inc.", "name": "PowerEdge R760",
        "machine_kind": "prebuilt", "components_submitted": "1",
    })

    run.refresh_from_db()
    assert len(run.excluded_component_ties) == 4


def test_a_post_without_the_marker_leaves_the_components_alone(client, submitter):
    """The reported bug, from the other side.

    A request that never rendered this section used to read as "untick everything": all three
    parts excluded and, for a vendor member, a declined claim recorded on each. Reported as the
    component boxes not being there, and the culprit on the devstack run was a verification
    script of mine that posted only the identity fields.

    A browser cannot produce this - it always posts the marker with the section - so what is
    being pinned is that a partial post is inert rather than destructive.
    """
    run = _run(submitter)
    run.excluded_component_ties = []
    run.component_overrides = {}
    run.save(update_fields=["excluded_component_ties", "component_overrides"])
    client.force_login(submitter)

    client.post(reverse("results:propose_listing", args=[run.uuid]), {
        "vendor_name": "Dell Inc.", "name": "PowerEdge R760",
        "machine_kind": "prebuilt",
    })

    run.refresh_from_db()
    assert run.excluded_component_ties == []
    assert run.component_overrides == {}


def test_the_summary_heading_and_its_subtitle_stack(client, submitter):
    """Reported as a spacing bug: the subtitle ran straight into "What this submission will do"
    with no gap.

    ``.card-header`` is a flex row in this Tabler build, so a title and a subtitle sitting in it
    as siblings lay out side by side. They need a block wrapper between them and the header.
    Structural rather than visual, because it is the shape that decides the layout and a
    stylesheet cannot be read from a screenshot.
    """
    run = _run(submitter)
    client.force_login(submitter)
    body = client.get(reverse("results:propose_listing", args=[run.uuid])).content.decode()

    header = body.index('id="submission-summary"')
    header = body.index('class="card-header"', header)
    title = body.index("card-title", header)

    assert "<div>" in body[header:title], (
        "the title and subtitle are direct children of a flex card-header, so they will "
        "render on one line"
    )


def test_the_marker_is_on_the_page(client, submitter):
    """It is only useful if the form actually renders it. Drop it from the template and every
    real component edit silently stops saving, which no other test would catch."""
    run = _run(submitter)
    client.force_login(submitter)

    body = client.get(reverse("results:propose_listing", args=[run.uuid])).content.decode()

    assert 'name="components_submitted"' in body


# --- the JavaScript -------------------------------------------------------------


def test_the_summary_javascript_checks_pass():
    """``shutil.which`` rather than a trial run: a missing executable makes
    ``subprocess.run`` raise rather than return non-zero, and ``check=False`` does not
    cover that."""
    if shutil.which("node") is None:
        pytest.skip("node is not available in this environment")

    script = Path(settings.BASE_DIR) / "tests" / "js" / "summary_check.js"
    result = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_the_script_reads_the_baseline_the_page_writes():
    """Source-level, because the wiring needs a DOM: the id in the template and the id the
    script looks up have to be the same string, and nothing else would notice."""
    source = (
        Path(settings.BASE_DIR) / "static" / "js" / "submission-summary.js"
    ).read_text()
    template = (
        Path(settings.BASE_DIR) / "templates" / "results" / "propose_listing.html"
    ).read_text()

    assert 'getElementById("submission-baseline")' in source
    assert 'json_script:"submission-baseline"' in template
    assert "[data-summary]" in source
    assert "data-summary" in template


# --- the duplicate-badge regression ---------------------------------------------
#
# Reported from the page: "when the page loads it shows that things match existing, but
# when values are changed it shows new - will be created, but the matches line is still
# there." Two badges on screen at once, contradicting each other.
#
# Cause: the script created the span itself and appended it to ``input.parentNode``.
# ``combobox.js`` then moves the input into a ``.combobox-wrap`` div of its own
# (``setup()``, "input.parentNode.insertBefore(wrap, input); wrap.appendChild(input)"), so
# the parent changed between refreshes. The lookup found nothing in the new parent, made a
# second badge there, and the first was left orphaned in the old one still reading
# "matches".
#
# The template owns the element now, so there is exactly one and its position never
# depends on which script ran first.


def test_the_template_renders_one_badge_per_text_field(client, submitter):
    import re

    run = _run(submitter)
    client.force_login(submitter)

    body = client.get(
        reverse("results:propose_listing", args=[run.uuid])
    ).content.decode()
    found = re.findall(r'data-match-badge="([^"]+)"', body)

    assert found, "no badge holders rendered"
    assert len(found) == len(set(found)), f"duplicate badge holders: {found}"
    # The two the summary actually fills in.
    assert "vendor_name" in found
    assert "name" in found


def test_the_script_never_creates_a_badge_element():
    """Source-level, because the bug needs two scripts and a DOM to reproduce.

    If this ever creates its own span again, the duplicate comes straight back the moment
    combobox.js reparents the input - and nothing else in the suite would notice, because
    both badges are individually correct.
    """
    source = (
        Path(settings.BASE_DIR) / "static" / "js" / "submission-summary.js"
    ).read_text()
    body = source[source.index("function badgeFor"):source.index("function init")]

    assert "createElement" not in body, (
        "badgeFor is creating its own element again; the template owns it"
    )
    assert "appendChild" not in body
    assert "data-match-badge=" in body, "it should look the holder up by field name"


def test_the_badge_holder_is_outside_the_combobox_wrap(client, submitter):
    """``combobox.js`` wraps only the input itself, so a holder rendered after it stays put.

    Asserted on order rather than on nesting, which is what a string comparison can see:
    the holder must come after the input in the markup, so reparenting the input cannot
    take the holder with it.
    """
    run = _run(submitter)
    client.force_login(submitter)

    body = client.get(
        reverse("results:propose_listing", args=[run.uuid])
    ).content.decode()

    assert body.index('name="vendor_name"') < body.index('data-match-badge="vendor_name"')
