"""Applying a naming rule to a catalog that already exists.

Saving a rule changes nothing; this is where it takes effect, and it shows the consequence first.
Three outcomes, and only one of them is safe without a person: a rename is unambiguous, a
collision is a merge, and a conflict means a rule matches more broadly than its author meant.
"""
from __future__ import annotations

import json
from urllib.parse import urlencode

import pytest
from django.contrib.auth.models import Group, User
from django.urls import reverse

from lumina.hardware.models import Component, ComponentKind, ComponentNamingRule
from lumina.results import ingest, naming
from lumina.results.tests import factories as f

pytestmark = pytest.mark.django_db

INTEL_MATCH = [{"field": "pci.vendor_id", "op": "eq", "value": "8086"}]
INTEL_IGPU = {"pci": "00:02.0", "driver": "i915",
              "pci_ids": {"vendor": "Intel Corporation [8086]",
                          "device": "Arrow Lake-S [Intel Graphics] [7d67]"}}


@pytest.fixture
def reviewer(client):
    user = User.objects.create_user("name-rev", password="pw")
    user.groups.add(Group.objects.get_or_create(name="reviewer")[0])
    client.force_login(user)
    return user


def _run(submitter=None, gpus=(INTEL_IGPU,), cpu="Intel(R) Core(TM) Ultra 9 275HX"):
    submitter = submitter or User.objects.create_user(f"sub-{User.objects.count()}-x")
    inventory = f.default_inventory()
    inventory["summary"]["gpus"] = list(gpus)
    inventory["summary"]["cpus"] = [{**inventory["summary"]["cpus"][0], "model": cpu}]
    return ingest.ingest_bundle(
        submitter=submitter, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"], inventory=inventory,
            results=[f.validate_result("validate.cpu.functional")],
        ))),
    )


def _linked_gpu(run):
    from lumina.results.services import ensure_component_ties

    ensure_component_ties(run)
    return run.listing_components.filter(kind=ComponentKind.gpu.value).first()


# --- the plan ---------------------------------------------------------------------


def test_a_new_rule_shows_up_as_a_rename_before_it_is_applied():
    run = _run()
    component = _linked_gpu(run)
    assert component is not None
    ComponentNamingRule.objects.create(
        kind="gpu", priority=1, conditions=INTEL_MATCH, build="Arc-era integrated")

    plan = naming.rename_plan()

    assert [row["name"] for row in plan["renames"]] == ["Arc-era integrated"]
    component.refresh_from_db()
    assert component.name != "Arc-era integrated", "the plan must not have written anything"


def test_applying_renames_and_keeps_the_old_name_as_an_alias():
    """So a run that reported the old name still matches this part rather than creating a second
    component under what it used to be called."""
    run = _run()
    component = _linked_gpu(run)
    was = component.name
    ComponentNamingRule.objects.create(
        kind="gpu", priority=1, conditions=INTEL_MATCH, build="Arc-era integrated")

    assert naming.apply_plan(naming.rename_plan()) == 1
    component.refresh_from_db()

    assert component.name == "Arc-era integrated"
    assert was in component.attributes["aliases"]


def test_a_name_already_taken_is_a_collision_not_a_rename():
    """The two may be the same part or may not, and nothing here can tell."""
    run = _run()
    component = _linked_gpu(run)
    Component.objects.create(
        kind=ComponentKind.gpu.value, name="Taken", vendor=component.vendor)
    ComponentNamingRule.objects.create(kind="gpu", priority=1, conditions=INTEL_MATCH, build="Taken")

    plan = naming.rename_plan()

    assert plan["renames"] == []
    assert [row["name"] for row in plan["collisions"]] == ["Taken"]


def test_a_collision_does_not_block_the_renames_beside_it():
    """One awkward part must not hold a good rule hostage."""
    first = _run()
    _linked_gpu(first)
    Component.objects.create(kind=ComponentKind.gpu.value, name="Taken",
                             vendor=_linked_gpu(first).vendor)
    ComponentNamingRule.objects.create(
        kind="gpu", priority=1, conditions=INTEL_MATCH,
        build="{% if cpu.model %}Taken{% else %}Other{% endif %}")

    plan = naming.rename_plan()

    assert naming.apply_plan(plan) == len(plan["renames"])


def test_runs_that_disagree_are_a_conflict_rather_than_a_coin_toss():
    """One part, two runs, two names. That usually means a rule matches more broadly than whoever
    wrote it meant, and picking one of the two would hide it."""
    first = _run()
    component = _linked_gpu(first)
    second = _run(gpus=(INTEL_IGPU,), cpu="Intel(R) Core(TM) i5-8250U")
    second.listing_components.add(component)
    ComponentNamingRule.objects.create(
        kind="gpu", priority=1, conditions=INTEL_MATCH, build="{{ cpu.model_normalized }} iGPU")

    plan = naming.rename_plan()

    assert plan["renames"] == []
    assert len(plan["conflicts"]) == 1
    assert len(plan["conflicts"][0]["names"]) == 2


def test_a_conflict_is_not_applied():
    first = _run()
    component = _linked_gpu(first)
    second = _run(gpus=(INTEL_IGPU,), cpu="Intel(R) Core(TM) i5-8250U")
    second.listing_components.add(component)
    was = component.name
    ComponentNamingRule.objects.create(
        kind="gpu", priority=1, conditions=INTEL_MATCH, build="{{ cpu.model_normalized }} iGPU")

    naming.apply_plan(naming.rename_plan())
    component.refresh_from_db()

    assert component.name == was


def test_a_part_already_named_as_the_rules_say_is_left_alone():
    run = _run()
    _linked_gpu(run)

    plan = naming.rename_plan()

    assert plan["renames"] == [] and plan["unchanged"] >= 1


# --- the page ---------------------------------------------------------------------


def test_the_plan_page_lists_what_would_change(client, reviewer):
    run = _run()
    _linked_gpu(run)
    ComponentNamingRule.objects.create(
        kind="gpu", priority=1, conditions=INTEL_MATCH, build="Arc-era integrated")

    body = client.get(reverse("review:naming_plan")).content.decode()

    assert "Arc-era integrated" in body
    assert "Apply these renames" in body


def test_applying_from_the_page_writes_the_names(client, reviewer):
    run = _run()
    component = _linked_gpu(run)
    ComponentNamingRule.objects.create(
        kind="gpu", priority=1, conditions=INTEL_MATCH, build="Arc-era integrated")

    client.post(reverse("review:naming_apply"), follow=True)
    component.refresh_from_db()

    assert component.name == "Arc-era integrated"


def test_only_a_reviewer_may_apply(client):
    client.force_login(User.objects.create_user("not-a-reviewer", password="pw"))

    assert client.post(reverse("review:naming_apply")).status_code == 403


# --- the live preview a reviewer writes against ------------------------------------


def test_the_preview_says_what_a_draft_rule_would_call_the_part(client, reviewer):
    run = _run()

    response = client.post(reverse("review:naming_preview"), {
        "kind": "gpu", "build": "Arc-era integrated",
        "run": str(run.uuid), "conditions": json.dumps(INTEL_MATCH),
    })
    body = response.json()

    assert body["ok"] is True
    assert body["devices"][0]["named"] == "Arc-era integrated"
    assert body["devices"][0]["matched"] is True


def test_the_preview_reports_a_broken_rule_rather_than_saving_it(client, reviewer):
    response = client.post(reverse("review:naming_preview"), {
        "kind": "gpu", "build": "{{ unclosed ", "conditions": json.dumps([]),
    })
    body = response.json()

    assert body["ok"] is False
    assert "build" in body["errors"]
    # The two the migration seeds, and not a third: a preview saves nothing.
    assert ComponentNamingRule.objects.filter(built_in=False).count() == 0


def test_the_preview_counts_what_the_rule_would_reach(client, reviewer):
    """"This looks right" and "this renames 412 machines" are different decisions, and only one
    of them is visible without asking."""
    run = _run()
    _linked_gpu(run)

    body = client.post(reverse("review:naming_preview"), {
        "kind": "gpu", "build": "Arc-era integrated",
        "run": str(run.uuid), "conditions": json.dumps(INTEL_MATCH),
    }).json()

    assert body["affected"] >= 1


def test_a_rule_that_matches_nothing_reaches_nothing(client, reviewer):
    run = _run()
    _linked_gpu(run)

    body = client.post(reverse("review:naming_preview"), {
        "kind": "gpu", "build": "never", "run": str(run.uuid),
        "conditions": json.dumps([{"field": "pci.vendor_id", "op": "eq", "value": "dead"}]),
    }).json()

    assert body["affected"] == 0
    assert body["devices"][0]["matched"] is False


# --- the same two actions wherever a reviewer is looking --------------------------
#
# The collected data under a validate run, a benchmark run, and a survey submission is the same
# data. A reviewer who can blacklist a part on one had no way to say it on the others, and could
# not write a naming rule anywhere but the Django admin, which a reviewer cannot reach.


def test_a_survey_submission_offers_the_same_blacklist(client, reviewer):
    from lumina.hardware.models import ComponentExclusionRule
    from lumina.survey.models import SurveySubmission

    sub = SurveySubmission.objects.create(
        origin=SurveySubmission.ORIGIN_SURVEY, trust_tier=SurveySubmission.TIER_VERIFIED,
        identity_hash="h", inventory={"summary": {"gpus": [INTEL_IGPU]}},
    )

    client.post(reverse("review:survey_blacklist_device", args=[sub.pk]),
                {"vendor_id": "8086", "device_id": "7d67", "kind": "gpu"}, follow=True)

    assert ComponentExclusionRule.objects.filter(vendor_id="8086", device_id="7d67").exists()


def test_the_survey_page_shows_the_actions(client, reviewer):
    from lumina.survey.models import SurveySubmission

    sub = SurveySubmission.objects.create(
        origin=SurveySubmission.ORIGIN_SURVEY, trust_tier=SurveySubmission.TIER_VERIFIED,
        identity_hash="h", inventory={"summary": {"gpus": [INTEL_IGPU]}},
    )

    body = client.get(reverse("review:survey_submission_detail", args=[sub.pk])).content.decode()

    assert reverse("review:survey_blacklist_device", args=[sub.pk]) in body
    assert reverse("review:naming_rule_new") in body


def test_a_run_page_offers_the_naming_action(client, reviewer):
    run = _run()

    body = client.get(reverse("review:run_detail", args=[run.pk])).content.decode()

    assert reverse("review:naming_rule_new") in body


def test_a_reviewer_can_save_a_rule_without_the_django_admin(client, reviewer):
    run = _run()

    client.post(reverse("review:naming_rule_new"), {
        "enabled": "on",
        "kind": "gpu", "model_pattern": "", "build": "Arc-era integrated", "fallback": "",
        "priority": "5", "notes": "from a review", "run": str(run.uuid),
        "conditions": json.dumps(INTEL_MATCH),
    }, follow=True)

    rule = ComponentNamingRule.objects.get(priority=5)
    assert rule.build == "Arc-era integrated"
    assert rule.created_by == reviewer


def test_saving_a_rule_renames_nothing_on_its_own(client, reviewer):
    """Two decisions: "I think this is right" and "do it to the catalog"."""
    run = _run()
    component = _linked_gpu(run)
    was = component.name

    client.post(reverse("review:naming_rule_new"), {
        "enabled": "on",
        "kind": "gpu", "build": "Arc-era integrated", "priority": "5",
        "run": str(run.uuid), "conditions": json.dumps(INTEL_MATCH),
    }, follow=True)
    component.refresh_from_db()

    assert component.name == was
    assert naming.rename_plan()["renames"], "but the plan now says it would"


def test_a_broken_rule_comes_back_to_the_form_rather_than_saving(client, reviewer):
    response = client.post(reverse("review:naming_rule_new"), {
        "enabled": "on",
        "kind": "gpu", "build": "{{ 1 + }}", "priority": "40",
    })

    assert response.status_code == 200
    assert not ComponentNamingRule.objects.filter(built_in=False).exists()


def test_only_a_reviewer_may_write_a_rule(client):
    client.force_login(User.objects.create_user("not-a-reviewer-2", password="pw"))

    assert client.get(reverse("review:naming_rule_new")).status_code == 403


# --- a family is not a part -------------------------------------------------------


def test_a_family_component_is_never_renamed():
    """A run links to the curated family where one exists, because certification is granted per
    generation. The plan proposed renaming "AMD Radeon RX 9000 Series (RDNA 4)" to one of the
    models inside it, which would flatten a generation into a single part."""
    from lumina.hardware.models import ComponentRole

    run = _run()
    component = _linked_gpu(run)
    component.role = ComponentRole.FAMILY
    component.name = "Intel Arc/Xe Series"
    component.save()
    ComponentNamingRule.objects.create(
        kind="gpu", priority=1, conditions=INTEL_MATCH, build="Arc-era integrated")

    plan = naming.rename_plan()

    assert plan["renames"] == []
    assert plan["collisions"] == []
    assert plan["conflicts"] == []


def test_a_model_component_is_still_renamed_beside_a_family():
    """The distinction is the role, not the presence of a family: a machine whose part has its own
    model entry still gets it corrected."""
    from lumina.hardware.models import Component, ComponentRole

    run = _run()
    model = _linked_gpu(run)
    assert model.role == ComponentRole.MODEL
    family = Component.objects.create(
        kind=ComponentKind.gpu.value, name="Intel Arc/Xe Series",
        vendor=model.vendor, role=ComponentRole.FAMILY)
    run.listing_components.add(family)
    ComponentNamingRule.objects.create(
        kind="gpu", priority=1, conditions=INTEL_MATCH, build="Arc-era integrated")

    plan = naming.rename_plan()

    assert [row["component"].pk for row in plan["renames"]] == [model.pk]


def test_with_two_model_components_the_plan_does_not_guess():
    """The one-component fallback is what catches a part whose component was already renamed, so
    it cannot be loosened to "pick the first": with two candidates, nothing says which."""
    from lumina.hardware.models import Component, ComponentRole

    run = _run()
    first = _linked_gpu(run)
    second = Component.objects.create(
        kind=ComponentKind.gpu.value, name="Some other card",
        vendor=first.vendor, role=ComponentRole.MODEL)
    run.listing_components.add(second)
    first.name = "renamed out of the way"
    first.save()

    assert naming.rename_plan()["renames"] == []


def test_the_fallback_still_finds_a_part_whose_component_was_renamed():
    """Which is what it is for: after an apply, the component is called the new name and the run
    still reported the old one."""
    run = _run()
    component = _linked_gpu(run)
    component.name = "Arc-era integrated"
    component.save()
    ComponentNamingRule.objects.create(
        kind="gpu", priority=1, conditions=INTEL_MATCH, build="Renamed again")

    assert [row["name"] for row in naming.rename_plan()["renames"]] == ["Renamed again"]


# --- the builder offers what the machine reports, not three named fields ------------
#
# Vendor and the PCI ids were three inputs of their own, each fixed to one operator. That made
# three of the hundreds of readable paths look like the only three, and "vendor contains" the only
# way to ask about a vendor.
#
# Asserted against the rendered context rather than the HTML: the page also prints a reference
# table of every field this machine reports, so "pci.device_id is somewhere in the body" stays
# true even when nothing prefilled it.


def _builder(client, run=None, **params):
    query = {"kind": "gpu", "vendor_id": "8086", "device_id": "7d67", **params}
    if run is not None:
        query["run"] = str(run.uuid)
    return client.get(reverse("review:naming_rule_new") + "?" + urlencode(query))


def _builder_for(client, submission, **params):
    return _builder(client, submission=submission.pk, **params)


def _fields(response) -> dict[str, str]:
    return {entry["path"]: entry["value"]
            for _group, entries in response.context["field_groups"] for entry in entries}


def test_the_builder_prefills_the_conditions_that_identify_the_part(client, reviewer):
    response = _builder(client, _run())

    assert response.context["conditions"] == [
        {"field": "pci.vendor_id", "op": "eq", "value": "8086"},
        {"field": "pci.device_id", "op": "eq", "value": "7d67"},
    ]


def test_the_prefilled_conditions_are_rendered_as_rows_to_edit(client, reviewer):
    """Prefilling is only worth anything if the rows arrive filled in."""
    body = _builder(client, _run()).content.decode()
    # Scoped to the rows themselves: the page also carries a blank row in a <template> for the
    # "Add a condition" button to clone, and counting that would pass with nothing prefilled.
    rows = body.partition('id="conditions"')[2].partition("</div>\n    <button")[0]

    assert rows.count('name="condition_field"') == 2, "one row per suggested condition"
    assert 'value="8086"' in rows and 'value="7d67"' in rows


def test_the_builder_prefills_the_name_the_part_has_now(client, reviewer):
    """So the common case is editing what is there, not composing from nothing."""
    response = _builder(client, _run())

    assert response.context["prefill"]["build"] == "Intel Core Ultra 9 275HX (Intel Graphics)"


def test_a_device_the_run_never_reported_prefills_nothing(client, reviewer):
    """The ids come off a query string, so they are worth checking against the run's own devices
    rather than trusted. Nothing is prefilled from another device, and no values are shown as
    though this one had reported them."""
    response = _builder(client, _run(), vendor_id="10de", device_id="2482")

    assert response.context["conditions"] == []
    fields = _fields(response)
    assert fields["pci.vendor_id"] == "" and fields["cpu.model"] == ""


def test_the_builder_lists_every_field_with_its_value(client, reviewer):
    """The values are the point: a reviewer should not have to guess which path holds 8086."""
    response = _builder(client, _run())
    fields = _fields(response)

    assert fields["pci.vendor_id"] == "8086"
    assert fields["cpu.model_normalized"] == "Intel Core Ultra 9 275HX"
    # The machine's other cards are in the context too, and are what the built-in AMD rule tests.
    assert fields["machine.gpus"] == "1"
    assert "Fields you can use" in response.content.decode()


# --- the fields are discoverable without knowing them first ------------------------
#
# They were offered only through a <datalist>, which suggests nothing until you type the start of
# a path. So a reviewer who did not already know there was a "pci.vendor_id" had no way to find
# out there was one, and the field that most needs the help - the name template - had no
# suggestion at all.


def test_the_fields_are_grouped_by_their_prefix(client, reviewer):
    """"What is there under pci" is the question somebody actually has."""
    groups = dict(_builder(client, _run()).context["field_groups"])

    assert {"device", "pci", "cpu", "machine"} <= set(groups)
    assert {entry["path"] for entry in groups["pci"]} >= {"pci.vendor_id", "pci.slot"}


def test_the_part_s_own_fields_come_first(client, reviewer):
    """They are what the rule is about, and alphabetical order buries them under cpu."""
    groups = [group for group, _entries in _builder(client, _run()).context["field_groups"]]

    assert groups[0] == "device"


def test_the_fields_are_listed_even_with_no_machine_to_read(client, reviewer):
    """Opened from the naming page there is no device, and "what can I match on" is still a
    question the form has to answer."""
    fields = _fields(client.get(reverse("review:naming_rule_new")))

    assert "pci.vendor_id" in fields
    assert fields["pci.vendor_id"] == "", "nothing reported it, so it has no value"


def test_each_field_carries_the_expression_a_template_needs(client, reviewer):
    """The path and the expression are the same thing right up until a key has a space in it, and
    a reviewer inserting one should not have to know which case they are in."""
    entries = {entry["path"]: entry["expr"]
               for _group, group_entries in _builder(client, _run()).context["field_groups"]
               for entry in group_entries}

    assert entries["pci.vendor_id"] == "{{ pci.vendor_id }}"


def test_the_datalist_offers_paths_not_values(client, reviewer):
    """With the value as the option label, Firefox filtered on the label: typing "pci" matched
    none of the pci fields and the suggestion list looked empty."""
    body = _builder(client, _run()).content.decode()
    datalist = body.partition("<datalist")[2].partition("</datalist>")[0]

    assert 'value="pci.vendor_id"' in datalist
    assert "8086" not in datalist


def test_every_operator_is_offered_not_just_contains(client, reviewer):
    body = client.get(reverse("review:naming_rule_new")).content.decode()

    for op in naming.CONDITION_OPS:
        assert f'value="{op}"' in body, op


def test_a_rule_saves_from_the_posted_condition_rows(client, reviewer):
    client.post(reverse("review:naming_rule_new"), {
        "enabled": "on",
        "kind": "gpu", "priority": "5", "build": "By condition rows",
        "condition_field": ["pci.vendor_id", "cpu.model"],
        "condition_op": ["eq", "icontains"],
        "condition_value": ["8086", "Ultra"],
    }, follow=True)

    assert ComponentNamingRule.objects.get(priority=5).conditions == [
        {"field": "pci.vendor_id", "op": "eq", "value": "8086"},
        {"field": "cpu.model", "op": "icontains", "value": "Ultra"},
    ]


def test_a_blank_condition_row_is_dropped_rather_than_matching_everything(client, reviewer):
    """An empty field would test nothing, and a rule that tests nothing applies to every part."""
    client.post(reverse("review:naming_rule_new"), {
        "enabled": "on",
        "kind": "gpu", "priority": "5", "build": "x",
        "condition_field": ["pci.vendor_id", "  "],
        "condition_op": ["eq", "eq"], "condition_value": ["8086", ""],
    }, follow=True)

    assert len(ComponentNamingRule.objects.get(priority=5).conditions) == 1


def test_a_padded_row_is_trimmed(client, reviewer):
    """A path with a space around it looks identical in the form and matches nothing."""
    client.post(reverse("review:naming_rule_new"), {
        "enabled": "on",
        "kind": "gpu", "priority": "5", "build": "x",
        "condition_field": [" pci.vendor_id "], "condition_op": ["eq"],
        "condition_value": [" 8086 "],
    }, follow=True)

    assert ComponentNamingRule.objects.get(priority=5).conditions == [
        {"field": "pci.vendor_id", "op": "eq", "value": "8086"},
    ]


def test_a_row_posted_without_its_operator_still_saves(client, reviewer):
    """Anything but the builder's own form can send short lists, and an IndexError here is a 500
    on a page that had a perfectly good rule in it."""
    client.post(reverse("review:naming_rule_new"), {
        "enabled": "on",
        "kind": "gpu", "priority": "5", "build": "x",
        "condition_field": ["pci.vendor_id"],
    }, follow=True)

    assert ComponentNamingRule.objects.get(priority=5).conditions == [
        {"field": "pci.vendor_id", "op": "eq", "value": ""},
    ]


def test_conditions_still_post_as_json(client, reviewer):
    """The shape the preview used before the rows existed, and what a script would send."""
    client.post(reverse("review:naming_rule_new"), {
        "enabled": "on",
        "kind": "gpu", "priority": "5", "build": "x",
        "conditions": json.dumps(INTEL_MATCH),
    }, follow=True)

    assert ComponentNamingRule.objects.get(priority=5).conditions == INTEL_MATCH


def test_a_rejected_rule_comes_back_with_the_rows_the_reviewer_typed(client, reviewer):
    """Rejecting the rule and clearing the form is how somebody loses their work."""
    response = client.post(reverse("review:naming_rule_new"), {
        "enabled": "on",
        "kind": "gpu", "priority": "5", "build": "{{ unclosed ",
        "condition_field": ["pci.vendor_id"], "condition_op": ["eq"],
        "condition_value": ["8086"],
    })

    assert response.context["conditions"] == INTEL_MATCH


def test_previewing_without_a_run_does_not_crash(client, reviewer):
    """The builder opened from the naming page has no run, and previewing with nothing to preview
    against is an ordinary thing to do. It reached a UUIDField as an empty string and raised."""
    response = client.post(reverse("review:naming_preview"), {
        "kind": "gpu", "build": "x", "run": "",
    })

    assert response.status_code == 200
    assert response.json() == {"ok": True, "devices": [], "affected": 0}


def test_previewing_with_a_nonsense_run_does_not_crash(client, reviewer):
    response = client.post(reverse("review:naming_preview"), {
        "kind": "gpu", "build": "x", "run": "not-a-uuid",
    })

    assert response.status_code == 200
    assert response.json()["ok"] is True


# --- the builder prefills from whichever page it was opened from -------------------
#
# The prefill read runs only, so on the survey page "Name it" opened an empty form - the one page
# whose whole job is looking at collected hardware.

INTEL_NIC = {"pci": "00:1f.6", "driver": "e1000e",
             "pci_ids": {"vendor": "Intel Corporation [8086]",
                         "device": "Ethernet Connection (17) I219-LM [0dc7]",
                         # The board it is soldered to, which is what a GPU would be named after
                         # and a NIC deliberately is not.
                         "subsystem_device": "X11DPi-N [0868]"}}


def _submission(devices=(INTEL_IGPU,), nics=()):
    from lumina.survey.models import SurveySubmission

    return SurveySubmission.objects.create(
        origin=SurveySubmission.ORIGIN_SURVEY, trust_tier=SurveySubmission.TIER_VERIFIED,
        identity_hash="h", cpu_model="Intel(R) Core(TM) Ultra 9 275HX",
        inventory={"summary": {"gpus": list(devices), "nics": list(nics)}},
    )


def test_the_builder_prefills_from_a_survey_submission(client, reviewer):
    sub = _submission()

    response = _builder_for(client, sub)

    assert response.context["conditions"] == [
        {"field": "pci.vendor_id", "op": "eq", "value": "8086"},
        {"field": "pci.device_id", "op": "eq", "value": "7d67"},
    ]
    assert response.context["prefill"]["build"] == "Intel Core Ultra 9 275HX (Intel Graphics)"


def test_the_survey_page_links_carry_the_submission(client, reviewer):
    """Without it the link lands on an empty form, which is the whole thing being fixed."""
    sub = _submission()

    body = client.get(reverse("review:survey_submission_detail", args=[sub.pk])).content.decode()

    assert f"submission={sub.pk}" in body


def test_a_nonsense_submission_prefills_nothing_rather_than_raising(client, reviewer):
    response = _builder(client, submission="not-a-number")

    assert response.status_code == 200
    assert response.context["conditions"] == []


def test_a_nic_prefills_with_its_own_name_not_a_gpu_derivation(client, reviewer):
    """Every kind the two pages offer the action for, not just the one the engine names. A GPU is
    named after its subsystem where it has one, which for an onboard NIC is the motherboard."""
    sub = _submission(devices=(), nics=(INTEL_NIC,))

    response = _builder_for(client, sub, kind="nic", device_id="0dc7")

    assert response.context["prefill"]["build"] == "Ethernet Connection (17) I219-LM"


def test_a_kind_nothing_consults_yet_is_marked_as_such(client, reviewer):
    """A rule for it saves, validates, and then does nothing, and the reviewer cannot tell that
    from a match that is simply wrong."""
    kinds = dict(client.get(reverse("review:naming_rule_new")).context["kinds"])

    assert "not applied yet" not in kinds["gpu"]
    assert "not applied yet" in kinds["nic"]


def test_a_rejected_rule_keeps_the_page_it_came_from(client, reviewer):
    """Otherwise Preview stops working the moment a rule is rejected once."""
    sub = _submission()

    response = client.post(reverse("review:naming_rule_new"), {
        "enabled": "on",
        "kind": "gpu", "priority": "5", "build": "{{ unclosed ", "submission": str(sub.pk),
    })

    assert response.context["source"] == {"submission": str(sub.pk)}


# --- a rule is a row somebody can go back to --------------------------------------
#
# Rules could be written and never changed. A rule that turns out too broad is exactly the rule
# somebody needs to narrow, and the only way to reach one was the Django admin, which a reviewer
# cannot open.


def _rule(**fields) -> ComponentNamingRule:
    return ComponentNamingRule.objects.create(**{
        "kind": "gpu", "priority": 5, "conditions": INTEL_MATCH, "build": "Arc-era integrated",
        **fields,
    })


def _edit(client, rule, **fields):
    return client.post(reverse("review:naming_rule_edit", args=[rule.pk]), {
        "enabled": "on", "kind": rule.kind, "priority": str(rule.priority),
        "build": rule.build, "model_pattern": rule.model_pattern, "notes": rule.notes,
        "condition_field": [c["field"] for c in rule.conditions],
        "condition_op": [c["op"] for c in rule.conditions],
        "condition_value": [c["value"] for c in rule.conditions],
        **fields,
    }, follow=True)


def test_the_form_opens_on_an_existing_rule(client, reviewer):
    rule = _rule(notes="written during a review")

    response = client.get(reverse("review:naming_rule_edit", args=[rule.pk]))

    assert response.context["prefill"]["build"] == "Arc-era integrated"
    assert response.context["prefill"]["notes"] == "written during a review"
    assert response.context["conditions"] == INTEL_MATCH


def test_editing_changes_the_rule_rather_than_adding_one(client, reviewer):
    rule = _rule()

    _edit(client, rule, build="Intel Arc 140V")

    rule.refresh_from_db()
    assert rule.build == "Intel Arc 140V"
    assert ComponentNamingRule.objects.filter(priority=5).count() == 1, "it added one instead"


def test_a_rule_can_be_narrowed_by_dropping_a_condition(client, reviewer):
    """The edit somebody actually makes: it matched more than they meant."""
    rule = _rule(conditions=[*INTEL_MATCH,
                             {"field": "pci.device_id", "op": "eq", "value": "7d67"}])

    _edit(client, rule, condition_field=["pci.device_id"], condition_op=["eq"],
          condition_value=["7d67"])

    rule.refresh_from_db()
    assert rule.conditions == [{"field": "pci.device_id", "op": "eq", "value": "7d67"}]


def test_a_rule_can_be_turned_off_without_being_lost(client, reviewer):
    """The reversible way to stop a rule: the row records a judgement and why it was made."""
    rule = _rule()
    _edit(client, rule, enabled="")

    rule.refresh_from_db()
    assert rule.enabled is False
    assert rule not in naming.active_rules()


def test_a_rule_that_is_off_can_be_turned_back_on(client, reviewer):
    rule = _rule(enabled=False)

    _edit(client, rule, enabled="on")

    rule.refresh_from_db()
    assert rule.enabled is True


def test_a_disabled_rule_is_still_listed(client, reviewer):
    """Hiding it makes it unreachable, and unreachable is not the same as off."""
    _rule(enabled=False, build="turned off")

    body = client.get(reverse("review:naming_plan")).content.decode()

    assert "turned off" in body


def test_every_listed_rule_links_to_its_editor(client, reviewer):
    rule = _rule()

    body = client.get(reverse("review:naming_plan")).content.decode()

    assert reverse("review:naming_rule_edit", args=[rule.pk]) in body


def test_a_broken_edit_does_not_save_over_the_rule(client, reviewer):
    """The saved rule keeps working while somebody fixes what they were typing."""
    rule = _rule()

    _edit(client, rule, build="{{ 1 + }}")

    rule.refresh_from_db()
    assert rule.build == "Arc-era integrated"


def test_a_priority_that_is_not_a_number_is_reported_not_a_crash(client, reviewer):
    """It reached int() straight off the form, and a typo took the whole page with it, along with
    everything else the reviewer had typed."""
    rule = _rule()

    response = _edit(client, rule, priority="soon")

    assert response.status_code == 200
    assert "whole number" in response.content.decode(), "it was accepted silently"
    rule.refresh_from_db()
    assert rule.priority == 5


def test_a_rule_can_be_deleted(client, reviewer):
    rule = _rule()

    client.post(reverse("review:naming_rule_delete", args=[rule.pk]), follow=True)

    assert not ComponentNamingRule.objects.filter(pk=rule.pk).exists()


def test_deleting_a_rule_leaves_the_parts_it_named_alone(client, reviewer):
    """A rename was applied to the catalog, and undoing it is its own decision."""
    from lumina.hardware.models import Component

    rule = _rule()
    component = _linked_gpu(_run())
    component.name, component.named_by = "Arc-era integrated", rule
    component.save(update_fields=["name", "named_by"])

    client.post(reverse("review:naming_rule_delete", args=[rule.pk]), follow=True)

    component.refresh_from_db()
    assert component.name == "Arc-era integrated"
    assert component.named_by is None
    assert Component.objects.filter(pk=component.pk).exists()


def test_deleting_takes_a_post(client, reviewer):
    """A link somebody follows, or a page a crawler opens, must not delete a rule."""
    rule = _rule()

    response = client.get(reverse("review:naming_rule_delete", args=[rule.pk]))

    assert response.status_code == 405
    assert ComponentNamingRule.objects.filter(pk=rule.pk).exists()


def test_editing_a_built_in_rule_says_what_it_is(client, reviewer):
    """They reproduce naming that used to be in code, so they are the ones most likely to be
    matching parts the reviewer is not looking at."""
    rule = _rule(built_in=True)

    body = client.get(reverse("review:naming_rule_edit", args=[rule.pk])).content.decode()

    assert "ships with lumina" in body


def test_editing_a_rule_that_is_gone_is_a_404(client, reviewer):
    assert client.get(reverse("review:naming_rule_edit", args=[9999])).status_code == 404


def test_only_a_reviewer_can_edit(client):
    rule = _rule()
    User.objects.create_user("nobody", password="pw")
    client.login(username="nobody", password="pw")

    assert client.get(reverse("review:naming_rule_edit", args=[rule.pk])).status_code in (302, 403)


# --- is this name the machine's, or something we did to it? ------------------------
#
# A name a rule produced, a name lumina's built-in behaviour produced, and a name straight off the
# machine looked identical on a review page. They are three different things to somebody deciding
# whether to write a rule.

AMD_APU = {"pci": "c1:00.0", "driver": "amdgpu",
           "pci_ids": {"vendor": "Advanced Micro Devices, Inc. [AMD/ATI] [1002]",
                       "device": "Phoenix1 [15bf]"}}
NVIDIA_CARD = {"pci": "01:00.0", "driver": "nvidia",
               "pci_ids": {"vendor": "NVIDIA Corporation [10de]",
                           "device": "AD104 [GeForce RTX 4070] [2786]"}}


def test_a_part_a_rule_renamed_says_which_rule(client, reviewer):
    """And links to it, because the reviewer who disagrees with the name is the one who should be
    able to change the rule that produced it."""
    rule = _rule(build="Arc-era integrated")
    run = _run()

    body = client.get(reverse("review:run_detail", args=[run.pk])).content.decode()

    assert "renamed by rule" in body
    assert reverse("review:naming_rule_edit", args=[rule.pk]) in body


def test_with_every_rule_off_no_part_claims_to_be_renamed(client, reviewer):
    """There is no naming behaviour under the rules any more, so nothing can have been renamed
    without a row saying so."""
    ComponentNamingRule.objects.update(enabled=False)
    run = _run()

    body = client.get(reverse("review:run_detail", args=[run.pk])).content.decode()

    assert "renamed by" not in body


def test_a_part_nothing_touched_says_nothing(client, reviewer):
    """A badge on every row is a badge nobody reads."""
    run = _run(gpus=(NVIDIA_CARD,))

    body = client.get(reverse("review:run_detail", args=[run.pk])).content.decode()

    assert "renamed by" not in body


def test_a_name_that_identifies_nothing_is_flagged(client, reviewer):
    """The case that wants a rule written: an AMD die codename, with no CPU string to resolve it
    and no rule that matches."""
    run = _run(gpus=(AMD_APU,), cpu="AMD Ryzen 7 7840U")

    body = client.get(reverse("review:run_detail", args=[run.pk])).content.decode()

    assert "identifies nothing" in body


def test_the_survey_page_says_the_same_thing(client, reviewer):
    """Both pages read the same collected data, and a badge on one and not the other is the
    divergence this whole area exists to remove."""
    _rule(build="Arc-era integrated")
    sub = _submission()

    body = client.get(reverse("review:survey_submission_detail", args=[sub.pk])).content.decode()

    assert "renamed by rule" in body


def test_the_survey_page_shows_the_resolved_name(client, reviewer):
    """It showed the raw lspci string while the run page beside it showed the resolved one, which
    is the same "two names for one machine" problem in a different place."""
    sub = _submission()

    body = client.get(reverse("review:survey_submission_detail", args=[sub.pk])).content.decode()

    assert "Intel Core Ultra 9 275HX (Intel Graphics)" in body


# --- a conflict has to lead somewhere ----------------------------------------------


def test_a_conflict_names_the_runs_that_disagree(client, reviewer):
    """It said two names disagreed and stopped there. The runs are the evidence and the run page
    is where the part can be renamed or a rule written, so a reviewer had nowhere to go."""
    first = _run()
    second = _run(gpus=(INTEL_IGPU,), cpu="Intel(R) Core(TM) i5-9400")
    component = _linked_gpu(first)
    second.listing_components.add(component)

    plan = naming.rename_plan()
    conflict = plan["conflicts"][0]

    assert {source["name"] for source in conflict["sources"]} == set(conflict["names"])
    assert {run.pk for source in conflict["sources"] for run in source["runs"]} == {
        first.pk, second.pk}


def test_the_conflict_table_links_to_those_runs(client, reviewer):
    first = _run()
    second = _run(gpus=(INTEL_IGPU,), cpu="Intel(R) Core(TM) i5-9400")
    second.listing_components.add(_linked_gpu(first))

    body = client.get(reverse("review:naming_plan")).content.decode()

    assert reverse("review:run_detail", args=[first.pk]) in body
    assert reverse("review:run_detail", args=[second.pk]) in body


def test_a_conflict_with_many_runs_lists_a_few_and_counts_the_rest(client, reviewer):
    """A part reported by four hundred machines needs a way in, not four hundred links."""
    first = _run()
    component = _linked_gpu(first)
    for _ in range(naming.CONFLICT_EXAMPLES + 2):
        # A different CPU, so these runs name the part differently from the first and the
        # disagreement is what puts it in the conflict list at all.
        _run(cpu="Intel(R) Core(TM) i5-9400").listing_components.add(component)

    sources = naming.rename_plan()["conflicts"][0]["sources"]
    biggest = max(sources, key=lambda source: len(source["runs"]) + source["more"])

    assert len(biggest["runs"]) == naming.CONFLICT_EXAMPLES
    # The true remainder, not "however many over the cap we happened to keep": deriving the count
    # from a capped list could only ever say one more, whatever the real number was.
    assert biggest["more"] == 2


# --- previewing a rule that already exists -----------------------------------------


def test_previewing_an_edit_does_not_call_the_rule_its_own_duplicate(client, reviewer):
    """The preview builds a throwaway rule to try, and one with no id looked to the ambiguity
    check like a second rule with the same match at the same priority. So Preview refused every
    rule that was already saved, which is exactly the set worth previewing."""
    rule = _rule()

    body = client.post(reverse("review:naming_preview"), {
        "kind": rule.kind, "priority": str(rule.priority), "build": rule.build,
        "rule": str(rule.pk),
        "condition_field": ["pci.vendor_id"], "condition_op": ["eq"], "condition_value": ["8086"],
    }).json()

    assert body["ok"] is True, body.get("errors")


def test_previewing_a_new_rule_still_catches_a_real_duplicate(client, reviewer):
    """The check is worth keeping: two enabled rules with the same match at the same priority
    leave which one applies undefined."""
    rule = _rule()

    body = client.post(reverse("review:naming_preview"), {
        "kind": rule.kind, "priority": str(rule.priority), "build": "something else",
        "condition_field": ["pci.vendor_id"], "condition_op": ["eq"], "condition_value": ["8086"],
    }).json()

    assert body["ok"] is False
    assert "priority" in body["errors"]


def test_the_edit_page_carries_its_rule_id_for_the_preview(client, reviewer):
    rule = _rule()

    body = client.get(reverse("review:naming_rule_edit", args=[rule.pk])).content.decode()

    assert f'name="rule" value="{rule.pk}"' in body


def test_a_nonsense_rule_id_previews_as_a_new_rule(client, reviewer):
    response = client.post(reverse("review:naming_preview"), {
        "kind": "gpu", "priority": "1", "build": "x", "rule": "not-a-number",
    })

    assert response.status_code == 200


# --- why there is nothing to apply -------------------------------------------------
#
# A new rule can produce a name for one run and leave another run naming the same part something
# else. The part is then held as a conflict and nothing is applied - and the page said "every part
# is already called what the rules say", which is the opposite of what had happened.


def _disagreeing_runs():
    first = _run()
    component = _linked_gpu(first)
    second = _run(cpu="Intel(R) Core(TM) i5-9400")
    second.listing_components.add(component)
    return component


def test_the_page_does_not_claim_everything_is_fine_while_a_part_is_held(client, reviewer):
    _disagreeing_runs()

    body = client.get(reverse("review:naming_plan")).content.decode()

    assert "Every part is already called what the rules say" not in body
    assert "Nothing can be applied without a decision first" in body


def test_the_page_still_says_so_when_there_is_genuinely_nothing_to_do(client, reviewer):
    """The reassuring message is right when it is right, and this is the state it is for."""
    _run()
    naming.apply_plan(naming.rename_plan())

    body = client.get(reverse("review:naming_plan")).content.decode()

    assert "Every part is already called what the rules say" in body


def test_a_conflict_says_which_rule_produced_each_name(client, reviewer):
    """The first question somebody asks looking at two names they did not expect.

    A template that reads the CPU, since a rule producing one fixed name for every machine cannot
    disagree with itself and so never lands here.
    """
    rule = _rule(build="{{ cpu.model }} iGPU")
    _disagreeing_runs()

    plan = naming.rename_plan()
    sources = plan["conflicts"][0]["sources"]

    assert {source["rule"] for source in sources} == {rule}
    assert len(sources) == 2


def _conflict_table(client) -> str:
    """Just the conflict card. The rule table above it links every rule by definition, so an
    assertion against the whole page passes whether or not the conflict names anything."""
    body = client.get(reverse("review:naming_plan")).content.decode()
    return body.partition("Needs a decision: runs disagree")[2].partition("</table>")[0]


def test_the_conflict_table_links_to_the_rule_behind_a_name(client, reviewer):
    rule = _rule(build="{{ cpu.model }} iGPU")
    _disagreeing_runs()

    assert reverse("review:naming_rule_edit", args=[rule.pk]) in _conflict_table(client)


def test_a_name_no_rule_produced_says_so(client, reviewer):
    """Otherwise the column is blank and blank reads as missing rather than as an answer."""
    component = _linked_gpu(_run())
    ComponentNamingRule.objects.update(enabled=False)
    _run(gpus=(NVIDIA_CARD,)).listing_components.add(component)

    assert "as the machine reported it" in _conflict_table(client)


def test_applying_records_which_rule_chose_the_name(client, reviewer):
    """It was looked up in a mapping no caller ever passed, so every applied rename recorded None
    and the catalog could not say why a part was called what it was."""
    # The rule after the component, or the part is created under the right name already and
    # there is no rename to record anything on.
    component = _linked_gpu(_run())
    rule = _rule(build="Arc-era integrated")

    naming.apply_plan(naming.rename_plan())

    component.refresh_from_db()
    assert component.name == "Arc-era integrated"
    assert component.named_by == rule


def test_a_rename_the_built_in_naming_made_records_no_rule():
    """Not a rule, and not a lie about one either."""
    component = _linked_gpu(_run())
    ComponentNamingRule.objects.update(enabled=False)
    component.name = "Something else"
    component.save(update_fields=["name"])

    naming.apply_plan(naming.rename_plan())

    component.refresh_from_db()
    assert component.named_by is None


# --- a blacklisted part is not evidence about a catalogued one ---------------------
#
# A run reporting a real card and a blacklisted one had the blacklisted device fall through to
# "there is one linked component, so it must be that one". It then proposed a name for the real
# card, the two disagreed, and the part sat in "runs disagree" where no edit to any rule could
# resolve it: the rule was right, the device was one nobody catalogues.

BMC_DISPLAY = {"pci": "07:00.0", "driver": "ast",
               "pci_ids": {"vendor": "ASPEED Technology, Inc. [1a03]",
                           "device": "ASPEED Graphics Family [2000]"}}


def test_a_blacklisted_device_proposes_no_name(client, reviewer):
    run = _run(gpus=(INTEL_IGPU, BMC_DISPLAY))
    component = _linked_gpu(run)

    names = {name for _component, name, _rule in naming._gpu_names(run)}

    assert "ASPEED Graphics Family" not in names
    assert component is not None


def test_the_real_card_is_still_named_beside_it(client, reviewer):
    """The blacklisted device is dropped, not the run."""
    run = _run(gpus=(INTEL_IGPU, BMC_DISPLAY))
    _linked_gpu(run)

    names = {name for _component, name, _rule in naming._gpu_names(run)}

    assert names == {"Core Ultra 9 275HX (Intel Graphics)"}


def test_a_blacklisted_device_does_not_hold_the_plan_in_a_conflict(client, reviewer):
    """The whole point: with it gone there is a rename to apply."""
    run = _run(gpus=(INTEL_IGPU, BMC_DISPLAY))
    _linked_gpu(run)
    _rule(build="Arc-era integrated")

    plan = naming.rename_plan()

    assert plan["conflicts"] == []
    assert [row["name"] for row in plan["renames"]] == ["Arc-era integrated"]


def test_a_blacklisted_part_somebody_included_anyway_is_still_named(client, reviewer):
    """Blacklisting unticks a part by default; a reviewer can tick it to include it. One that was
    included is catalogued like any other and has to keep being named like any other."""
    from lumina.hardware.models import Component, ComponentKind, ComponentRole
    from lumina.results.component_match import catalog_name
    from lumina.results.services import _vendor_for, tieable_gpus

    run = _run(gpus=(BMC_DISPLAY,))
    device = tieable_gpus(run)[0]
    vendor = _vendor_for(device["vendor"])
    # Named as the catalog would name it: creating a component strips the vendor prefix the model
    # repeats, so the row for this part reads "Graphics Family" under the ASPEED vendor.
    catalogued = catalog_name(vendor, device["model"], ComponentKind.gpu)
    run.listing_components.add(Component.objects.create(
        vendor=vendor, name=catalogued, slug="aspeed-graphics-family",
        kind=ComponentKind.gpu.value, role=ComponentRole.MODEL))

    names = {name for _component, name, _rule in naming._gpu_names(run)}

    assert catalogued in names


def test_an_unmatched_device_is_not_guessed_at_when_there_are_several(client, reviewer):
    """"One component, so this device must be it" is only true when there is one device too."""
    second = {"pci": "02:00.0", "driver": "nvidia",
              "pci_ids": {"vendor": "NVIDIA Corporation [10de]",
                          "device": "AD104 [GeForce RTX 4070] [2786]"}}
    run = _run(gpus=(INTEL_IGPU, second))
    run.listing_components.set(
        run.listing_components.filter(name="Core Ultra 9 275HX (Intel Graphics)"))
    _linked_gpu(run)
    run.listing_components.remove(
        *run.listing_components.filter(kind="gpu").exclude(
            name="Core Ultra 9 275HX (Intel Graphics)"))

    proposed = {name for _component, name, _rule in naming._gpu_names(run)}

    assert "GeForce RTX 4070" not in proposed, "it was assigned to the Intel component"


def test_a_reviewer_can_point_the_pattern_at_another_field(client, reviewer):
    """The whole reason the field exists: naming a part after something in the CPU string used to
    need a code change, because a pattern could only ever look at the part's own model."""
    client.post(reverse("review:naming_rule_new"), {
        "enabled": "on", "kind": "gpu", "priority": "5", "build": "{{ match.1 }}",
        "match_field": "cpu.model", "model_pattern": r"^(\S+)",
    }, follow=True)

    rule = ComponentNamingRule.objects.get(priority=5)
    assert (rule.match_field, rule.model_pattern) == ("cpu.model", r"^(\S+)")


def test_the_edit_form_brings_the_pattern_field_back(client, reviewer):
    rule = _rule(match_field="cpu.model", model_pattern=r"^(\S+)")

    response = client.get(reverse("review:naming_rule_edit", args=[rule.pk]))

    assert response.context["prefill"]["match_field"] == "cpu.model"
    assert 'name="match_field"' in response.content.decode()


def test_the_builder_explains_how_to_write_a_pattern(client, reviewer):
    """The field list says what there is to match on and nothing about how. Groups are the only
    reason to write a pattern here at all, and they are where somebody gets stuck."""
    body = client.get(reverse("review:naming_rule_new")).content.decode()

    assert "Writing the pattern" in body
    assert "match.igpu" in body, "the named-group example"
    assert "(?:" in body, "the non-capturing example"


# --- CPUs and motherboards go through the rules too ---------------------------------
#
# Only GPU naming consulted the table. A rule for any other kind saved, validated, and did
# nothing, which is how an Ampere Altra stayed "Ampere(R) Altra(R) Processor" with its real model
# sitting unread in a DMI field.

ALTRA = "Ampere(R) Altra(R) Processor"


def _cpu_run(cpu=ALTRA):
    return _run(cpu=cpu)


def test_a_cpu_rule_renames_the_catalogued_part():
    run = _cpu_run()
    from lumina.results.services import ensure_component_ties

    ensure_component_ties(run)
    component = run.listing_components.filter(kind="cpu", role="model").first()
    assert component is not None
    ComponentNamingRule.objects.create(
        kind="cpu", priority=1, model_pattern=r"Altra", build="Ampere Altra Q80-30")

    plan = naming.rename_plan()

    assert [row["name"] for row in plan["renames"]] == ["Ampere Altra Q80-30"]


def test_a_cpu_rule_records_which_rule_named_it():
    from lumina.results.services import ensure_component_ties

    run = _cpu_run()
    ensure_component_ties(run)
    component = run.listing_components.filter(kind="cpu", role="model").first()
    rule = ComponentNamingRule.objects.create(
        kind="cpu", priority=1, model_pattern=r"Altra", build="Ampere Altra Q80-30")

    naming.apply_plan(naming.rename_plan())

    component.refresh_from_db()
    assert component.named_by == rule


def test_a_cpu_family_is_not_renamed():
    """Certification is granted per generation and a family is named by whoever curated it, so a
    rule about one machine's reported string must not flatten a generation into one part."""
    from lumina.hardware.models import ComponentRole

    run = _cpu_run()
    from lumina.results.services import ensure_component_ties

    ensure_component_ties(run)
    run.listing_components.filter(kind="cpu").update(role=ComponentRole.FAMILY)
    ComponentNamingRule.objects.create(kind="cpu", priority=1, build="flattened")

    assert naming.rename_plan()["renames"] == []


def test_a_new_cpu_is_catalogued_under_its_ruled_name():
    """Not only renamed afterwards: a machine arriving now should file the part correctly the
    first time."""
    from lumina.results.services import ensure_component_ties

    ComponentNamingRule.objects.create(
        kind="cpu", priority=1, model_pattern=r"Altra", build="Ampere Altra Q80-30")
    run = _cpu_run()

    ensure_component_ties(run)

    assert run.listing_components.filter(
        kind="cpu", name="Ampere Altra Q80-30", role="model").exists()


def test_a_human_correction_outranks_a_rule():
    """Somebody holding the machine who typed what the part is beats a pattern written about
    machines in general."""
    from lumina.results.services import component_tie_targets, tie_key

    run = _cpu_run()
    ComponentNamingRule.objects.create(kind="cpu", priority=1, build="from a rule")
    run.component_overrides = {tie_key("cpu", ALTRA): {"model": "what I typed"}}
    run.save(update_fields=["component_overrides"])

    entry = next(t for t in component_tie_targets(run) if t["kind"].value == "cpu")

    assert entry["raw_model"] == "what I typed"


def test_a_rule_does_not_move_the_key_a_decision_is_filed_under():
    """An exclusion, a declined vendor claim, and a brand fix are all filed against the reported
    string. Deriving the key from a ruled name would unpin every one of them."""
    from lumina.results.services import component_tie_targets, tie_key

    run = _cpu_run()
    ComponentNamingRule.objects.create(kind="cpu", priority=1, build="renamed by a rule")

    entry = next(t for t in component_tie_targets(run) if t["kind"].value == "cpu")

    assert entry["key"] == tie_key("cpu", ALTRA)


def test_a_motherboard_rule_reaches_the_board():
    from lumina.results.services import component_tie_targets

    run = _cpu_run()
    ComponentNamingRule.objects.create(
        kind="motherboard", priority=1, build="{{ vendor }} board")

    entry = next(t for t in component_tie_targets(run) if t["kind"].value == "motherboard")

    assert entry["raw_model"].endswith("board")


def test_the_run_page_offers_a_rule_for_a_cpu(client, reviewer):
    """A CPU carries no PCI ids, and the offer used to sit inside the blacklist's guard - which
    is keyed on exactly those ids."""
    run = _cpu_run()

    body = client.get(reverse("review:run_detail", args=[run.pk])).content.decode()

    assert "kind=cpu" in body


def test_the_builder_prefills_from_a_cpu(client, reviewer):
    run = _cpu_run()

    response = client.get(reverse("review:naming_rule_new")
                          + f"?kind=cpu&run={run.uuid}")

    assert response.context["prefill"]["build"] == ALTRA
    fields = {entry["path"]: entry["value"]
              for _group, entries in response.context["field_groups"] for entry in entries}
    assert fields["model"] == ALTRA


def test_the_builder_prefills_from_a_motherboard(client, reviewer):
    run = _cpu_run()

    response = client.get(reverse("review:naming_rule_new")
                          + f"?kind=motherboard&run={run.uuid}")

    assert response.context["prefill"]["build"] == run.board_model


def test_cpu_is_no_longer_marked_as_unread(client, reviewer):
    kinds = dict(client.get(reverse("review:naming_rule_new")).context["kinds"])

    assert "not applied yet" not in kinds["cpu"]
    assert "not applied yet" not in kinds["motherboard"]
    assert "not applied yet" in kinds["nic"], "that one still is"


def test_a_motherboard_rule_shows_up_in_the_plan():
    """The tie targets are where a new run gets its name; this is where the ones already in the
    catalog get theirs."""
    from lumina.results.services import ensure_component_ties

    run = _cpu_run()
    ensure_component_ties(run)
    board = run.listing_components.filter(kind="motherboard", role="model").first()
    assert board is not None
    ComponentNamingRule.objects.create(
        kind="motherboard", priority=1, build="Renamed board")

    plan = naming.rename_plan()

    assert [row["component"].pk for row in plan["renames"]] == [board.pk]
    assert [row["name"] for row in plan["renames"]] == ["Renamed board"]


def test_a_ruled_name_is_filed_under_the_catalog_s_spelling():
    """Creating a component strips the vendor prefix a name repeats, so a plan built on the raw
    rule output compares against a string no component is ever called."""
    from lumina.results.services import ensure_component_ties

    run = _cpu_run()
    ensure_component_ties(run)
    ComponentNamingRule.objects.create(
        kind="cpu", priority=1, build="Intel Xeon Gold 6430")

    assert [row["name"] for row in naming.rename_plan()["renames"]] == ["Xeon Gold 6430"]


def test_a_part_the_run_never_reported_is_not_renamed_to_nothing():
    """A component linked from an earlier state with no string on the run to name it. Without the
    guard the rules run against an empty model and propose renaming it to the vendor alone."""
    from lumina.results.services import ensure_component_ties

    run = _cpu_run()
    ensure_component_ties(run)
    run.cpu_model = ""
    run.save(update_fields=["cpu_model"])
    ComponentNamingRule.objects.create(kind="cpu", priority=1, build="{{ model }}")

    # The CPU only: blanking the column also takes the CPU name off the machine's iGPU, which is
    # the seeded Intel rule working exactly as it should.
    renamed = [row for row in naming.rename_plan()["renames"]
               if row["component"].kind == "cpu"]
    assert renamed == []


def test_a_kind_no_rule_reaches_is_offered_nothing(client, reviewer):
    """NICs are enumerated and shown on the same page, and nothing reads a rule for one."""
    run = _cpu_run()

    body = client.get(reverse("review:run_detail", args=[run.pk])).content.decode()

    assert "kind=nic" not in body


# --- the preview has to try the rule on the right parts -----------------------------
#
# It previewed a run's GPUs whatever the rule was about, so a CPU rule was tried on graphics cards
# and reported "Matches nothing on this run" however right it was. A preview that lies is worse
# than no preview, and this is exactly the rule a reviewer is least able to check by eye: one
# lumina wrote for them from a submitter's answer.


def _preview(client, run, **fields):
    return client.post(reverse("review:naming_preview"), {
        "priority": "50", "run": str(run.uuid), **fields}).json()


def test_a_cpu_rule_is_previewed_against_the_cpu(client, reviewer):
    run = _cpu_run()

    body = _preview(client, run, kind="cpu", model_pattern=r"^(Ampere).*", build="{{ match.1 }}")

    assert [d["named"] for d in body["devices"]] == ["Ampere"]
    assert body["devices"][0]["matched"] is True


def test_a_motherboard_rule_is_previewed_against_the_board(client, reviewer):
    run = _cpu_run()

    body = _preview(client, run, kind="motherboard", build="Renamed board")

    assert [d["named"] for d in body["devices"]] == ["Renamed board"]


def test_a_gpu_rule_is_still_previewed_against_the_gpus(client, reviewer):
    run = _cpu_run()

    body = _preview(client, run, kind="gpu", conditions=json.dumps(INTEL_MATCH),
                    build="Arc-era integrated")

    assert [d["named"] for d in body["devices"]] == ["Arc-era integrated"]


def test_a_rule_with_no_kind_is_previewed_against_every_kind_it_reaches(client, reviewer):
    run = _cpu_run()

    body = _preview(client, run, kind="", build="Everything")

    assert len(body["devices"]) >= 3, "the gpu, the cpu, and the board"


def test_the_count_reaches_a_cpu_rule_too(client, reviewer):
    """It counted GPUs only, so a CPU rule always reported nought catalogued parts - beside a
    device list that also said nothing, which together read as "this rule does nothing"."""
    from lumina.results.services import ensure_component_ties

    run = _cpu_run()
    ensure_component_ties(run)

    body = _preview(client, run, kind="cpu", model_pattern=r"^(Ampere).*",
                    build="Ampere Altra Q80-30")

    assert body["affected"] >= 1


def test_the_count_is_about_this_rule_and_not_the_table(client, reviewer):
    from lumina.results.services import ensure_component_ties

    run = _cpu_run()
    ensure_component_ties(run)

    body = _preview(client, run, kind="cpu", model_pattern=r"^(nothing matches this)$",
                    build="x")

    assert body["affected"] == 0


def test_the_count_finds_a_part_by_what_it_is_called_now(client, reviewer):
    """Not by what the draft would call it. A machine with two cards has two components, so the
    "one of each, it must be that one" fallback cannot rescue a lookup that asks for a name
    nothing is called yet - and the count silently reads nought."""
    from lumina.hardware.models import Component, ComponentRole
    from lumina.results.services import ensure_component_ties
    from lumina.vendors.models import Vendor

    run = _run()
    ensure_component_ties(run)
    # A second model-role GPU on the run, so "one component and one device, it must be that one"
    # cannot rescue a lookup that asked for a name nothing is called yet.
    run.listing_components.add(Component.objects.create(
        vendor=Vendor.objects.create(name="Some Other Vendor", slug="sov"),
        name="Unrelated card", slug="unrelated-card",
        kind=ComponentKind.gpu.value, role=ComponentRole.MODEL))

    body = _preview(client, run, kind="gpu", conditions=json.dumps(INTEL_MATCH),
                    build="Arc-era integrated")

    assert body["affected"] == 1, "the Intel one, found under the name it has today"
