"""A submitter says a part is wrong, and which of their machine's values is right.

Their vendor and model boxes are gone. Free text there landed in ``component_overrides``, which
names the catalog component, so a typo minted a catalog entry - and once reviewers had naming
rules, the same correction made on their side fixes every machine of that shape rather than one
run. See ``test_component_corrections`` for the boxes, which are now the reviewer's.

What is left is the thing only the person holding the machine can do: say the detection is wrong,
and pick which of the values their own firmware reported is the real one. They pick a value, never
a field. The path it came from rides along unseen, and it is what lets a reviewer turn one answer
about one machine into a rule.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import Group, User
from django.urls import reverse

from lumina.hardware.models import NameSuggestionField
from lumina.results import ingest, services
from lumina.results.forms import RunListingProposalForm
from lumina.results.tests import factories as f

pytestmark = pytest.mark.django_db

ALTRA_DMI = {"processor": [{"props": {
    "Manufacturer": "Ampere(R)", "Version": "Ampere(R) Altra(R) Processor",
    "Part Number": "Q80-30", "Core Count": "80",
}}]}


@pytest.fixture
def submitter():
    return User.objects.create_user("sug-sub", password="pw")


@pytest.fixture
def reviewer(client):
    user = User.objects.create_user("sug-rev", password="pw")
    user.groups.add(Group.objects.get_or_create(name="reviewer")[0])
    return user


def _run(submitter, dmi=None, cpu="Ampere(R) Altra(R) Processor"):
    inventory = f.default_inventory()
    inventory["summary"]["cpus"] = [{**inventory["summary"]["cpus"][0], "model": cpu}]
    if dmi is not None:
        inventory["summary"]["fields"] = {"dmi": dmi}
    return ingest.ingest_bundle(
        submitter=submitter, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"], inventory=inventory,
            results=[f.validate_result("validate.cpu.functional")],
        ))),
    )


def _row(run, submitter, kind="cpu"):
    form = RunListingProposalForm(run=run, user=submitter)
    index = next(i for i, r in enumerate(form.component_rows) if r["kind"] == kind)
    return form, index, form.component_rows[index]


def _post(client, run, **extra):
    return client.post(reverse("results:propose_listing", args=[run.uuid]), {
        "vendor_name": "Dell Inc.", "name": "PowerEdge R760", "machine_kind": "prebuilt",
        "components_submitted": "1",
        "included_ties": [e["key"] for e in services.preview_component_ties(run)],
        **extra,
    })


# --- what the submitter is asked --------------------------------------------------


def test_a_part_with_something_else_to_offer_gets_the_question(submitter):
    run = _run(submitter, ALTRA_DMI)

    _, _, row = _row(run, submitter)

    assert [c["value"] for c in row["candidates"]] == ["Q80-30"]
    assert row.get("suggest_field") is not None


def test_a_part_with_nothing_else_to_offer_gets_no_control(submitter):
    """No empty list behind a disclosure: the page sends them to the notes field instead."""
    run = _run(submitter, {})

    _, _, row = _row(run, submitter)

    assert row["candidates"] == []
    assert row.get("suggest_field") is None


def test_a_kind_no_rule_reaches_is_not_asked_about(submitter):
    """An answer about a NIC would go nowhere: nothing reads a naming rule for one. Configured
    with a row that would otherwise match, so this tests the guard rather than an empty table."""
    NameSuggestionField.objects.create(
        kind="nic", path="pci.subsystem_device", label="Board the chip is on", priority=10)
    run = _run(submitter, ALTRA_DMI)

    _, _, row = _row(run, submitter, kind="nic")

    assert row["candidates"] == []
    assert row.get("suggest_field") is None


def test_the_question_is_closed_until_they_open_it(client, submitter):
    """Asked of every part on every run it is noise. Behind the disclosure it is one question."""
    run = _run(submitter, ALTRA_DMI)
    client.force_login(submitter)

    body = client.get(reverse("results:propose_listing", args=[run.uuid])).content.decode()

    assert "Not this part?" in body
    assert "<details" in body
    assert "Q80-30" in body, "rendered, just closed"


def test_the_page_offers_the_notes_field_when_there_is_nothing_to_pick(client, submitter):
    run = _run(submitter, {})
    client.force_login(submitter)

    body = client.get(reverse("results:propose_listing", args=[run.uuid])).content.decode()

    assert "reported no other name for it" in body


# --- what gets stored -------------------------------------------------------------


def test_picking_a_value_records_it_with_where_it_came_from(client, submitter):
    """The path is the half that makes this worth more than a note."""
    run = _run(submitter, ALTRA_DMI)
    form, index, row = _row(run, submitter)
    client.force_login(submitter)

    _post(client, run, **{f"{form.COMPONENT_SUGGEST_PREFIX}{index}": "0"})

    run.refresh_from_db()
    assert run.component_suggestions[row["key"]] == {
        "path": "dmi.processor.0.Part Number", "value": "Q80-30",
    }


def test_saying_it_is_right_records_nothing(client, submitter):
    run = _run(submitter, ALTRA_DMI)
    form, index, _ = _row(run, submitter)
    client.force_login(submitter)

    _post(client, run, **{f"{form.COMPONENT_SUGGEST_PREFIX}{index}": ""})

    run.refresh_from_db()
    assert run.component_suggestions == {}


def test_something_else_records_nothing_structured(client, submitter):
    """It is a prose answer, and prose belongs in the notes field that already exists rather than
    in a column nobody can act on."""
    run = _run(submitter, ALTRA_DMI)
    form, index, _ = _row(run, submitter)
    client.force_login(submitter)

    _post(client, run, **{f"{form.COMPONENT_SUGGEST_PREFIX}{index}": "other",
                          "submitter_notes": "It is an Ampere Altra Q80-30."})

    run.refresh_from_db()
    assert run.component_suggestions == {}
    assert "Q80-30" in run.submitter_notes


def test_a_hand_made_post_cannot_invent_a_value(client, submitter):
    """The answer is an index into a list the server built, so a posted string is not a path the
    submitter gets to choose - and an index past the end is not one of the choices either."""
    run = _run(submitter, ALTRA_DMI)
    form, index, _ = _row(run, submitter)
    client.force_login(submitter)

    response = _post(client, run, **{f"{form.COMPONENT_SUGGEST_PREFIX}{index}": "99"})

    assert response.status_code == 200, "refused, not saved and not a crash"
    run.refresh_from_db()
    assert run.component_suggestions == {}


def test_a_suggestion_names_nothing_on_its_own(client, submitter):
    """It is not a correction. The catalog goes on calling the part what it was called until a
    reviewer acts."""
    run = _run(submitter, ALTRA_DMI)
    form, index, row = _row(run, submitter)
    client.force_login(submitter)

    _post(client, run, **{f"{form.COMPONENT_SUGGEST_PREFIX}{index}": "0"})

    run.refresh_from_db()
    assert run.component_overrides.get(row["key"], {}).get("model") is None
    entry = next(e for e in services.preview_component_ties(run) if e["kind"] == "cpu")
    assert entry["raw_model"] == "Ampere(R) Altra(R) Processor"


# --- what the reviewer sees -------------------------------------------------------


def test_the_reviewer_is_shown_the_answer_and_its_source(client, submitter, reviewer):
    run = _run(submitter, ALTRA_DMI)
    form, index, _ = _row(run, submitter)
    client.force_login(submitter)
    _post(client, run, **{f"{form.COMPONENT_SUGGEST_PREFIX}{index}": "0"})
    client.force_login(reviewer)

    body = client.get(reverse("review:run_detail", args=[run.pk])).content.decode()

    assert "The submitter says this is" in body and "Q80-30" in body
    assert "dmi.processor.0.Part Number" in body


def test_the_rule_builder_opens_prefilled_from_the_answer(client, submitter, reviewer):
    """One answer about one machine becomes a fix for every machine of that shape, which is the
    whole reason the path was recorded rather than the string alone."""
    run = _run(submitter, ALTRA_DMI)
    form, index, row = _row(run, submitter)
    client.force_login(submitter)
    _post(client, run, **{f"{form.COMPONENT_SUGGEST_PREFIX}{index}": "0"})
    client.force_login(reviewer)

    response = client.get(reverse("review:naming_rule_new"), {
        "kind": "cpu", "run": str(run.uuid), "suggested": row["key"]})
    prefill = response.context["prefill"]

    assert prefill["match_field"] == "dmi.processor.*.Part Number"
    assert prefill["model_pattern"] == r"^(Q80\-30)$"
    assert prefill["build"] == "{{ match.1 }}"


def test_the_prefilled_rule_names_the_part_the_submitter_said(client, submitter, reviewer):
    """End to end: the pattern the builder offers has to actually match, or the reviewer is handed
    a rule that does nothing."""
    from lumina.hardware.models import ComponentNamingRule
    from lumina.results.component_match import name_status

    run = _run(submitter, ALTRA_DMI)
    form, index, row = _row(run, submitter)
    client.force_login(submitter)
    _post(client, run, **{f"{form.COMPONENT_SUGGEST_PREFIX}{index}": "0"})
    client.force_login(reviewer)
    prefill = client.get(reverse("review:naming_rule_new"), {
        "kind": "cpu", "run": str(run.uuid), "suggested": row["key"]}).context["prefill"]
    ComponentNamingRule.objects.create(
        kind="cpu", priority=1, match_field=prefill["match_field"],
        model_pattern=prefill["model_pattern"], build=prefill["build"])

    named = name_status("cpu", "Ampere", "Ampere(R) Altra(R) Processor", run.inventory)

    assert named["name"] == "Q80-30"


def test_a_builder_opened_without_a_suggestion_is_unchanged(client, submitter, reviewer):
    run = _run(submitter, ALTRA_DMI)
    client.force_login(reviewer)

    prefill = client.get(reverse("review:naming_rule_new"), {
        "kind": "cpu", "run": str(run.uuid)}).context["prefill"]

    assert prefill["match_field"] == "" and prefill["model_pattern"] == ""


def test_a_suggestion_key_that_means_nothing_is_ignored(client, submitter, reviewer):
    run = _run(submitter, ALTRA_DMI)
    client.force_login(reviewer)

    prefill = client.get(reverse("review:naming_rule_new"), {
        "kind": "cpu", "run": str(run.uuid), "suggested": "cpu:nothing"}).context["prefill"]

    assert prefill["match_field"] == ""


def test_a_configured_field_can_be_turned_off_and_the_question_goes(submitter):
    """The control you asked for: which values are offered is a row, not a release."""
    NameSuggestionField.objects.filter(kind="cpu").update(enabled=False)
    run = _run(submitter, ALTRA_DMI)

    _, _, row = _row(run, submitter)

    assert row["candidates"] == []


def test_the_reviewer_is_asked_only_once(client, submitter, reviewer):
    """Two prompts reading "Not this part?" sat on the review page: the reviewer's own correction
    override, and the submitter's question, which keyed on the part's kind rather than on who was
    looking. Same words, different jobs, one above the other."""
    run = _run(submitter, {})
    client.force_login(reviewer)

    body = client.get(reverse("review:run_detail", args=[run.pk])).content.decode()

    assert "reported no other name for it" not in body, "the submitter's prompt"
    assert "Not this part?" not in body.replace("Not this part? Correct it", "")


def test_the_submitter_is_still_asked(client, submitter):
    """The other half: the question belongs on their page and nowhere else."""
    run = _run(submitter, {})
    client.force_login(submitter)

    body = client.get(reverse("results:propose_listing", args=[run.uuid])).content.decode()

    assert "reported no other name for it" in body
    assert "Not this part? Correct it" not in body, "the reviewer's control"
