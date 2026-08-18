"""Approving posts the component answers that are on the page.

Reported as: "the box for certify as Intel was ticked, so how is anything reversed?" The engine has
since caught up with the control, and an unanswered box is honoured as the claim it is offered as.
That fixed the under-grant and made the over-grant worse, because the two directions run through
the same silence.

The review page had two forms. The component controls sat in one with a "Save component changes"
button; Approve sat in the other. Nothing about the tie controls was in the approve POST, so a
reviewer who unticked "Certify as Intel" and pressed Approve certified the part at the vendor tier
they had just declined. The untick was not overruled, it was never sent.

They are one form now (``#run-review``), joined by ``form=`` on the approve buttons, and the views
persist what arrives before deciding anything.

These tests post what a browser would post, built by reading the rendered page and collecting the
controls that actually belong to that form. Asserting on a hand-written payload would pass no
matter what the template said, which is the whole failure being fixed here: the view was never the
problem.
"""
from __future__ import annotations

from html.parser import HTMLParser

import pytest
from django.contrib.auth.models import Group, User
from django.urls import reverse

from lumina.core.certification import ValidationLevel
from lumina.hardware.models import CommunityAttestation
from lumina.releases.models import AlmaLinuxRelease
from lumina.results import ingest, services
from lumina.results.tests import factories as f
from lumina.vendors.models import Vendor, VendorMembership

pytestmark = pytest.mark.django_db

FORM_ID = "run-review"


class _FormControls(HTMLParser):
    """The controls a browser would submit with ``#run-review``, and how they are attached.

    Ownership follows the HTML rule the fix relies on: a control belongs to the form named by its
    own ``form`` attribute if it has one, and otherwise to the form it is nested inside. Getting
    this right is the point - a test that just collected every input on the page would pass with
    the attribute deleted.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0                      # how deep we are inside <form id="run-review">
        self.data: dict[str, list[str]] = {}
        self.by_attribute: set[str] = set()  # names attached via form="run-review"
        self.form_ids: list[str] = []
        self.buttons: list[dict] = []
        self._textarea: str | None = None

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        if tag == "form":
            self.form_ids.append(attr.get("id", ""))
            if attr.get("id") == FORM_ID:
                self.depth = 1
            elif self.depth:
                self.depth += 1              # a nested form would be a bug; count it honestly
            return
        owner = attr.get("form")
        mine = owner == FORM_ID or (owner is None and self.depth > 0)
        if tag == "button":
            if mine:
                self.buttons.append(attr)
            return
        if not mine or not attr.get("name"):
            return
        if owner == FORM_ID:
            self.by_attribute.add(attr["name"])
        if tag == "textarea":
            self._textarea = attr["name"]
            self.data.setdefault(attr["name"], [])
            return
        if tag != "input":
            return
        kind = (attr.get("type") or "text").lower()
        if kind in {"checkbox", "radio"} and "checked" not in attr:
            return                           # unticked boxes are not posted, which is the premise
        if kind == "submit":
            return
        # ``on`` for a valueless checkbox, which is what a browser sends and what Django's
        # ``CheckboxInput`` needs: it reads "" as **False**, so collecting the missing attribute
        # as an empty string would post every ticked box as a decline and the tests would read
        # as proof of a bug that is not there.
        default = "on" if kind == "checkbox" else ""
        self.data.setdefault(attr["name"], []).append(attr.get("value", default))

    def handle_endtag(self, tag):
        if tag == "form" and self.depth:
            self.depth -= 1
        if tag == "textarea":
            self._textarea = None


def _form(client, reviewer, run) -> _FormControls:
    client.force_login(reviewer)
    page = client.get(reverse("review:run_detail", args=[run.pk]))
    assert page.status_code == 200
    parser = _FormControls()
    parser.feed(page.content.decode())
    return parser


def _payload(parser: _FormControls, **overrides) -> dict:
    """What the browser sends. ``key=None`` drops a control, which is what unticking does."""
    data = {name: list(values) for name, values in parser.data.items()}
    for key, value in overrides.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    return data


@pytest.fixture(autouse=True)
def releases():
    for major in (9, 10):
        AlmaLinuxRelease.objects.get_or_create(major=major, defaults={"supported": True})


@pytest.fixture
def intel():
    vendor, _ = Vendor.objects.get_or_create(name="Intel", defaults={"published": True})
    vendor.verified = True
    vendor.save(update_fields=["verified"])
    return vendor


@pytest.fixture
def reviewer():
    user = User.objects.create_user("apca-rev", password="pw")
    group, _ = Group.objects.get_or_create(name="reviewer")
    user.groups.add(group)
    return user


@pytest.fixture
def engineer(intel):
    user = User.objects.create_user("apca-intel", password="pw")
    VendorMembership.objects.create(
        user=user, vendor=intel, role=VendorMembership.ROLE_SUBMITTER,
    )
    return user


def _run(engineer, *, results=None, **report_kw):
    run = ingest.ingest_bundle(
        submitter=engineer, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"],
            results=results or [f.validate_result("validate.cpu.functional")],
            **report_kw,
        ))),
    )
    run.status = run.STATUS_PENDING
    run.save(update_fields=["status"])
    return run


def _levels() -> dict:
    out = {}
    for a in CommunityAttestation.objects.select_related("version"):
        listing = a.version.listing_system or a.version.listing_component
        out[str(listing)] = a.level
    return out


def _claim_names(parser: _FormControls) -> list[str]:
    return sorted(n for n in parser.data if n.startswith("tie_claim"))


CPU_FAMILY = "Intel Intel Xeon Scalable 4th Generation"


# --- the reported hole -------------------------------------------------------------


def test_the_claim_boxes_are_on_the_page_and_ticked(client, engineer, reviewer):
    """The premise for everything below. If they ever stop being offered ticked, the tests that
    untick them would pass by accident."""
    parser = _form(client, reviewer, _run(engineer))

    assert _claim_names(parser), "no vendor claim control was rendered"


def test_unticking_and_pressing_approve_declines_the_claim(client, engineer, reviewer):
    """One POST, no second button. This is the whole report."""
    run = _run(engineer)
    parser = _form(client, reviewer, run)
    claims = _claim_names(parser)
    data = _payload(parser, **{name: None for name in claims}, notes="")

    response = client.post(reverse("review:run_approve", args=[run.pk]), data)

    assert response.status_code == 302
    run.refresh_from_db()
    assert run.status == run.STATUS_APPROVED
    assert _levels()[CPU_FAMILY] == ValidationLevel.COMMUNITY


def test_leaving_them_ticked_still_grants_the_claim(client, engineer, reviewer):
    """The other direction, so the fix cannot be "drop every claim on approval"."""
    run = _run(engineer)
    parser = _form(client, reviewer, run)

    client.post(reverse("review:run_approve", args=[run.pk]), _payload(parser, notes=""))

    assert _levels()[CPU_FAMILY] == ValidationLevel.VENDOR


def test_the_untick_reaches_every_run_in_the_group(client, engineer, reviewer):
    """"Approve all N runs of this machine" is one button and one page, so the answers given on
    that page are answers about the machine. Applying them to the run that happened to be on
    screen and to none of the others granted the claim three times over."""
    first = _run(engineer, version_id="9.6")
    second = _run(
        engineer, version_id="10.1",
        run_id="dddddddd-0000-0000-0000-000000000001",
    )
    parser = _form(client, reviewer, first)
    claims = _claim_names(parser)
    data = _payload(parser, **{name: None for name in claims}, group_notes="")

    client.post(reverse("review:run_approve_group", args=[first.pk]), data)

    for run in (first, second):
        run.refresh_from_db()
        assert run.status == run.STATUS_APPROVED, "the group approval did not go through"
    levels = _levels()
    assert levels[CPU_FAMILY] == ValidationLevel.COMMUNITY
    # One attestation per (release, person), so the second run is proved through its own release
    # row rather than a second entry against the first.
    majors = {
        a.version.release.major for a in CommunityAttestation.objects.select_related("version")
    }
    assert majors == {9, 10}
    assert all(
        a.level == ValidationLevel.COMMUNITY
        for a in CommunityAttestation.objects.filter(
            version__listing_component__name="Intel Xeon Scalable 4th Generation"
        )
    )


def test_a_blocked_sibling_is_left_alone(client, engineer, reviewer):
    """A run that did not pass is not swept into the group, and must not be written to either.

    The reviewer is being told to open it on its own page. Recording this run's answers against it
    first would mean they arrive at a decision already made, about a run they have not seen and
    evidence they have not read.
    """
    passing = _run(engineer, version_id="9.6")
    failing = _run(
        engineer, version_id="10.1",
        run_id="dddddddd-0000-0000-0000-000000000005",
        results=[f.validate_result("validate.cpu.functional", status="fail")],
    )
    assert failing.verdict() is False, "the premise"
    before = dict(failing.component_overrides)

    parser = _form(client, reviewer, passing)
    data = _payload(parser, **{name: None for name in _claim_names(parser)}, group_notes="")
    client.post(reverse("review:run_approve_group", args=[passing.pk]), data)

    failing.refresh_from_db()
    assert failing.status == failing.STATUS_PENDING
    assert failing.component_overrides == before


# --- how the two are joined --------------------------------------------------------


def test_the_approve_buttons_are_controls_of_the_component_form(client, engineer, reviewer):
    """Structural, because the wiring is the fix. A view that persists what it is sent is no use
    if the button that submits is attached to a different form."""
    run = _run(engineer, version_id="9.6")
    _run(engineer, version_id="10.1", run_id="dddddddd-0000-0000-0000-000000000002")
    parser = _form(client, reviewer, run)

    assert parser.form_ids.count(FORM_ID) == 1, "the id must be unique to be referenceable"
    actions = {
        b.get("formaction") for b in parser.buttons if b.get("form") == FORM_ID
    }
    assert reverse("review:run_approve", args=[run.pk]) in actions
    assert reverse("review:run_approve_group", args=[run.pk]) in actions
    # And the save button is still there: nested in the form rather than joined by attribute, and
    # with no formaction, so it goes to the form's own endpoint as it always did.
    assert any(
        b.get("form") is None and b.get("formaction") is None for b in parser.buttons
    )


def test_one_notes_box_serves_both_buttons(client, engineer, reviewer):
    """There were two, and the second was added to stop a name collision once both belonged to
    one form. Two boxes was the wrong answer to that: whichever one the reviewer typed in,
    pressing the other button discarded it without a word. One box, used by whichever button is
    pressed, which is what the card looks like it does anyway."""
    run = _run(engineer, version_id="9.6")
    _run(engineer, version_id="10.1", run_id="dddddddd-0000-0000-0000-000000000003")
    parser = _form(client, reviewer, run)

    assert [n for n in parser.by_attribute if "notes" in n] == ["notes"]

    data = _payload(parser, notes="applies to both")
    client.post(reverse("review:run_approve_group", args=[run.pk]), data)

    run.refresh_from_db()
    assert run.reviewer_notes == "applies to both"


def test_plain_notes_still_reach_the_group(client, engineer, reviewer):
    """The fallback. Every existing caller posts ``notes``, including the API tests."""
    run = _run(engineer, version_id="9.6")
    _run(engineer, version_id="10.1", run_id="dddddddd-0000-0000-0000-000000000004")
    client.force_login(reviewer)

    client.post(reverse("review:run_approve_group", args=[run.pk]), {"notes": "from a caller"})

    run.refresh_from_db()
    assert run.reviewer_notes == "from a caller"


# --- what must not break -----------------------------------------------------------


def test_a_post_without_the_section_keeps_the_stored_answers(client, engineer, reviewer):
    """A bare approval - the API, a script, a page that never rendered the controls - carries no
    marker, and silence must not read as "untick everything". Same guard as the submitter's form."""
    run = _run(engineer)
    parser = _form(client, reviewer, run)
    claims = _claim_names(parser)
    client.post(
        reverse("review:run_component_ties", args=[run.pk]),
        _payload(parser, **{name: None for name in claims}),
    )
    run.refresh_from_db()
    stored = dict(run.component_overrides)
    assert stored, "the decline was not saved, so this proves nothing"

    client.post(reverse("review:run_approve", args=[run.pk]), {"notes": ""})

    run.refresh_from_db()
    assert run.component_overrides == stored
    assert _levels()[CPU_FAMILY] == ValidationLevel.COMMUNITY


def test_a_run_with_nothing_to_tie_can_still_be_approved(client, engineer, reviewer):
    """The form is rendered whether or not there are components, because the Approve button names
    it by id. An id that resolves to nothing detaches the button from every form, and it would
    quietly stop working on exactly the runs with the least to look at."""
    run = _run(engineer)
    run.cpu_model = ""
    run.board_vendor = ""
    run.board_model = ""
    run.gpu_model = ""
    run.inventory = {}
    run.save(update_fields=["cpu_model", "board_vendor", "board_model", "gpu_model",
                            "inventory"])
    run.listing_proposal = {"vendor_name": "Dell Inc.", "name": "PowerEdge R760",
                            "machine_kind": "prebuilt"}
    run.save(update_fields=["listing_proposal"])
    parser = _form(client, reviewer, run)

    assert parser.form_ids.count(FORM_ID) == 1
    assert any(
        b.get("form") == FORM_ID
        and b.get("formaction") == reverse("review:run_approve", args=[run.pk])
        for b in parser.buttons
    )

    response = client.post(
        reverse("review:run_approve", args=[run.pk]), _payload(parser, notes=""),
    )

    assert response.status_code == 302
    run.refresh_from_db()
    assert run.status == run.STATUS_APPROVED


# --- what joining the forms broke, found by auditing the change ---------------------
#
# Making Approve a control of the component form means every approval is also a save of that
# form. That is the point, and it is also how each of these got in: a defect that used to need
# somebody to press Save twice now happens on the ordinary path, at the moment the catalog entry
# is minted.


def _board_field(parser: _FormControls) -> str:
    """The model box prefilled with the Dell board the fixture reports."""
    return next(
        name for name, values in parser.data.items()
        if name.startswith("tie_model") and values and "0M83RH" in values[0].upper()
    )


def test_a_correction_survives_the_approval(client, engineer, reviewer):
    """A reviewer fixes the board model, presses Save, then presses Approve, and the catalog
    entry is created from the report anyway.

    ``component_overrides()`` records a value only where it differs from what the form showed.
    Once a correction is stored the form shows *that*, so echoing the prefill compared equal and
    the correction was rebuilt away. The reviewer is often the only person who knows that DMI's
    "0M83RH" is wrong, and approving is the one moment the name becomes permanent.
    """
    from lumina.hardware.models import Component

    run = _run(engineer)
    parser = _form(client, reviewer, run)
    field = _board_field(parser)
    client.post(
        reverse("review:run_component_ties", args=[run.pk]),
        _payload(parser, **{field: "PowerEdge R760 System Board"}),
    )
    run.refresh_from_db()
    key = next(k for k in run.component_overrides if k.startswith("motherboard:"))
    assert run.component_overrides[key]["model"] == "PowerEdge R760 System Board"

    reloaded = _form(client, reviewer, run)
    assert reloaded.data[field] == ["PowerEdge R760 System Board"], "the premise: it prefills"
    client.post(reverse("review:run_approve", args=[run.pk]), _payload(reloaded, notes=""))

    run.refresh_from_db()
    assert run.component_overrides[key]["model"] == "PowerEdge R760 System Board"
    assert list(
        Component.objects.filter(kind="motherboard").values_list("name", flat=True)
    ) == ["PowerEdge R760 System Board"]


def test_typing_back_what_the_report_said_still_undoes_a_correction(client, engineer, reviewer):
    """The other half. Preserving an echoed prefill must not make a correction permanent: typing
    the reported string back is the documented way to withdraw one."""
    run = _run(engineer)
    parser = _form(client, reviewer, run)
    field = _board_field(parser)
    client.post(
        reverse("review:run_component_ties", args=[run.pk]),
        _payload(parser, **{field: "Wrong Guess"}),
    )

    reloaded = _form(client, reviewer, run)
    client.post(
        reverse("review:run_component_ties", args=[run.pk]),
        _payload(reloaded, **{field: "0M83RH"}),
    )

    run.refresh_from_db()
    assert not any(
        "model" in v for k, v in run.component_overrides.items()
        if k.startswith("motherboard:")
    )


def test_a_siblings_own_decline_is_not_overturned(client, engineer, reviewer):
    """The group merge started as a plain ``dict.update``, so the run on screen won every field.

    A reviewer who declined the claim on run B, then opened run A where the box merely renders
    ticked by default and pressed "Approve all", had B's recorded decline replaced by A's
    silence. Silence must never outrank an answer.
    """
    first = _run(engineer, version_id="9.6")
    second = _run(
        engineer, version_id="10.1", run_id="dddddddd-0000-0000-0000-000000000006",
    )
    parser = _form(client, reviewer, second)
    client.post(
        reverse("review:run_component_ties", args=[second.pk]),
        _payload(parser, **{name: None for name in _claim_names(parser)}),
    )
    second.refresh_from_db()
    key = next(k for k in second.component_overrides if k.startswith("cpu:"))
    assert second.component_overrides[key]["attribute_to"] == "", "the premise"

    # Run A, untouched, where the box renders ticked.
    client.post(
        reverse("review:run_approve_group", args=[first.pk]),
        _payload(_form(client, reviewer, first), notes=""),
    )

    second.refresh_from_db()
    assert second.component_overrides[key]["attribute_to"] == ""
    assert all(
        a.level == ValidationLevel.COMMUNITY
        for a in CommunityAttestation.objects.filter(
            version__release__major=10,
            version__listing_component__name="Intel Xeon Scalable 4th Generation",
        )
    )


def _with_cpu(model: str) -> dict:
    inventory = f.make_report()["inventory"]
    inventory["summary"]["cpus"][0]["model"] = model
    return inventory


def test_a_decline_travels_when_the_sibling_names_the_part_differently(
    client, engineer, reviewer,
):
    """Tie keys are built from the reported model string, so two runs of one machine can file the
    same part under different keys. Keyed on the string alone, the reviewer's decline reached the
    run on screen and missed the sibling, which then certified at the tier just declined. Both
    resolve to the same CPU family, and that is what the answer is really about.
    """
    first = _run(engineer, version_id="9.6")
    second = _run(
        engineer, version_id="10.1", run_id="dddddddd-0000-0000-0000-000000000007",
        inventory=_with_cpu("Intel(R) Xeon(R) Gold 6448Y"),
    )
    assert second.cpu_model != first.cpu_model, "the premise: different strings"
    families = {
        e["component"] for run in (first, second)
        for e in services.preview_component_ties(run) if e["kind"] == "cpu"
    }
    assert len(families) == 1, "the premise: one family behind both"

    parser = _form(client, reviewer, first)
    client.post(
        reverse("review:run_approve_group", args=[first.pk]),
        _payload(parser, **{name: None for name in _claim_names(parser)}, notes=""),
    )

    assert _levels()[CPU_FAMILY] == ValidationLevel.COMMUNITY
    assert all(
        a.level == ValidationLevel.COMMUNITY
        for a in CommunityAttestation.objects.filter(
            version__listing_component__name="Intel Xeon Scalable 4th Generation",
        )
    ), "the sibling certified at the tier the reviewer declined"


def test_a_refused_approval_changes_nothing(client, engineer, reviewer):
    """Approving writes the answers first, so a refusal used to leave them written.

    ``approve_run`` turns runs down for reasons a reviewer meets in the ordinary course, and
    being told nothing happened while something did is the worst of both outcomes. Reproduced
    here with the simplest refusal there is: a run that is not open for review any more.
    """
    run = _run(engineer)
    parser = _form(client, reviewer, run)
    client.post(reverse("review:run_approve", args=[run.pk]), _payload(parser, notes=""))
    run.refresh_from_db()
    assert run.status == run.STATUS_APPROVED
    before = dict(run.component_overrides)

    stale = _payload(parser, **{name: None for name in _claim_names(parser)}, notes="")
    response = client.post(reverse("review:run_approve", args=[run.pk]), stale)

    assert response.status_code == 302
    run.refresh_from_db()
    assert run.component_overrides == before


def test_writing_to_a_sibling_is_logged(client, engineer, reviewer):
    """The group merge writes to runs nobody opened. Every diagnosis of a wrong tier in this
    system has started from the audit trail, so a change with no entry naming who made it and
    where it came from is the hardest kind to account for later."""
    from lumina.audit.models import AuditLogEntry

    first = _run(engineer, version_id="9.6")
    second = _run(
        engineer, version_id="10.1", run_id="dddddddd-0000-0000-0000-000000000008",
    )
    parser = _form(client, reviewer, first)

    client.post(
        reverse("review:run_approve_group", args=[first.pk]),
        _payload(parser, **{name: None for name in _claim_names(parser)}, notes=""),
    )

    entry = AuditLogEntry.objects.filter(
        action="test_run.component_ties_shared", target_id=str(second.pk),
    ).first()
    assert entry is not None
    assert entry.actor == reviewer
    assert entry.after["from_run"] == str(first.pk)
