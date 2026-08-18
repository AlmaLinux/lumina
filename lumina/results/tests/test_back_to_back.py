"""Several AlmaLinux versions of one machine, submitted before any approval.

The expected pattern for a vendor: upload 8, 9, and 10 results back to back, then
wait for review. Each run arrived as its own draft demanding its own listing
details, so the submitter answered the same questions once per bundle - and any
variation in what they typed forked the catalog, because no listing exists to
auto-link against until the first approval lands.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import Group, User
from django.urls import reverse

from lumina.hardware.models import System
from lumina.results import ingest, services
from lumina.results.models import TestRun
from lumina.results.tests import factories as f
from lumina.vendors.models import Vendor

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def releases():
    from lumina.releases.models import AlmaLinuxRelease

    for major in (8, 9, 10):
        AlmaLinuxRelease.objects.get_or_create(major=major,
                                               defaults={"supported": True})


@pytest.fixture
def submitter():
    return User.objects.create_user("b2b", email="b2b@example.com")


@pytest.fixture
def reviewer():
    user = User.objects.create_user("b2b-rev", email="b2br@example.com")
    group, _ = Group.objects.get_or_create(name="reviewer")
    user.groups.add(group)
    return user


def _run(submitter, run_id, version):
    Vendor.objects.get_or_create(name="Dell Inc.", defaults={"slug": "dell-inc"})
    inventory = f.default_inventory()
    inventory["summary"]["system"] = {"vendor": "Dell Inc.", "product": "PowerEdge R7715",
                                      "kind": "prebuilt", "bios": {}}
    inventory["summary"]["baseboard"] = {"vendor": "Dell Inc.", "product": "0ABC12"}
    report = f.make_report(
        run_types=["validate"], run_id=run_id, version_id=version,
        results=[f.validate_result("validate.cpu.functional")],
        inventory=inventory,
    )
    return ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(f.build_bundle(report)),
        source="api",
    )


def _three(submitter):
    return [
        _run(submitter, f"aaaaaaaa-0000-0000-0000-00000000000{i}", v)
        for i, v in enumerate(("9.6", "10.2", "8.10"))
    ]


DETAILS = {
    "vendor_name": "Dell Inc.", "name": "PowerEdge R7715",
    "machine_kind": "prebuilt", "model_number": "", "description": "",
    "vendor_spec_url": "", "cpu_model": "Xeon Gold 6430", "submitter_notes": "",
}


# --- answering once ------------------------------------------------------------


def test_answering_one_run_covers_the_others(client, submitter):
    runs = _three(submitter)
    for run in runs:
        assert services.missing_submission_details(run)

    client.force_login(submitter)
    client.post(reverse("results:propose_listing", args=[runs[0].uuid]), DETAILS)

    for run in runs:
        run.refresh_from_db()
        assert services.missing_submission_details(run) == [], run.alma_release


def test_the_submitter_is_told_it_happened(client, submitter):
    """Changing pages someone is not looking at has to be visible."""
    runs = _three(submitter)
    client.force_login(submitter)

    response = client.post(
        reverse("results:propose_listing", args=[runs[0].uuid]), DETAILS, follow=True
    )

    text = " ".join(str(m) for m in response.context["messages"])
    assert "2 other unsubmitted run(s)" in text


def test_the_second_form_opens_prefilled(client, submitter):
    runs = _three(submitter)
    client.force_login(submitter)
    client.post(reverse("results:propose_listing", args=[runs[0].uuid]), DETAILS)

    body = client.get(
        reverse("results:propose_listing", args=[runs[1].uuid])
    ).content.decode()

    assert 'value="PowerEdge R7715"' in body
    # The CPU is not a top-of-page field any more (it is corrected in the components section for a
    # detected CPU), so the prefill that carries across the group is the shared listing identity.


def test_the_run_page_says_siblings_are_waiting(client, submitter):
    runs = _three(submitter)
    client.force_login(submitter)

    body = client.get(runs[0].get_absolute_url()).content.decode()

    assert "other unsubmitted run(s) of this" in body


def test_notes_stay_per_run(client, submitter):
    """They describe the run, not the machine."""
    runs = _three(submitter)
    client.force_login(submitter)

    client.post(reverse("results:propose_listing", args=[runs[0].uuid]),
                {**DETAILS, "submitter_notes": "Ran with SMT off."})

    runs[1].refresh_from_db()
    assert runs[1].submitter_notes == ""


def test_a_bounced_run_is_not_overwritten(client, submitter, reviewer):
    """It carries a specific request; a sibling's answer must not discard it."""
    runs = _three(submitter)
    client.force_login(submitter)
    client.post(reverse("results:propose_listing", args=[runs[2].uuid]),
                {**DETAILS, "name": "Reviewer asked for this name"})
    runs[2].refresh_from_db()
    services.submit_for_review(runs[2], by=submitter)
    services.request_run_changes(runs[2], by=reviewer, reason="Fix the name.")

    client.post(reverse("results:propose_listing", args=[runs[0].uuid]), DETAILS)

    runs[2].refresh_from_db()
    assert runs[2].listing_proposal["name"] == "Reviewer asked for this name"


# --- one machine, one listing --------------------------------------------------


def test_three_runs_approved_together_produce_one_listing(submitter, reviewer):
    runs = _three(submitter)
    for run in runs:
        run.listing_proposal = dict(DETAILS)
        run.save(update_fields=["listing_proposal"])
        services.submit_for_review(run, by=submitter)

    assert System.objects.count() == 0        # nothing exists until approval
    for run in runs:
        services.approve_run(run, by=reviewer)

    assert System.objects.count() == 1
    system = System.objects.get()
    assert {r.listing_system_id for r in
            (run.refresh_from_db() or run for run in runs)} == {system.pk}


def test_differently_typed_proposals_still_converge(submitter, reviewer):
    """The failure this was built for: two listings for one machine, because the
    second submission spelled the name differently and nothing existed to match
    against when it was submitted."""
    runs = _three(submitter)
    names = ["PowerEdge R7715", "Dell PowerEdge R7715", "PowerEdge R7715"]
    for run, name in zip(runs, names, strict=True):
        run.listing_proposal = {**DETAILS, "name": name}
        run.save(update_fields=["listing_proposal"])
        services.submit_for_review(run, by=submitter)
    for run in runs:
        services.approve_run(run, by=reviewer)

    assert System.objects.count() == 1, list(
        System.objects.values_list("vendor__name", "name")
    )


def test_each_release_counts_once_for_this_submitter(submitter, reviewer):
    """Three runs on three releases from one person are three attestations.

    This test used to assert 1, on the rule that one submitter is one independent
    confirmation however many times they run the suite. That rule threw away the
    thing the catalog most wants to know: 8, 9, and 10 are three different claims
    about three different releases, and collapsing them lost two of the three.

    The dedup is still there, now per (release, person) - see
    ``test_per_major_attestation.py``, where a repeat run on the *same* release
    still counts once.
    """
    runs = _three(submitter)
    for run in runs:
        run.listing_proposal = dict(DETAILS)
        run.save(update_fields=["listing_proposal"])
        services.submit_for_review(run, by=submitter)
        services.approve_run(run, by=reviewer)

    system = System.objects.get()
    assert system.attestation_count == 3
    # One listing, three proven releases - the reason for submitting several.
    assert sorted(
        v.release.major for v in system.versions.select_related("release")
    ) == [8, 9, 10]
    assert all(v.attestations.count() == 1 for v in system.versions.all())


# --- submitting the machine's runs together -------------------------------------


def test_the_group_submit_sends_every_draft_of_the_machine(client, submitter):
    runs = _three(submitter)
    client.force_login(submitter)
    client.post(reverse("results:propose_listing", args=[runs[0].uuid]), DETAILS)

    response = client.post(
        reverse("results:submit_group_for_review", args=[runs[0].uuid]), follow=True
    )

    for run in runs:
        run.refresh_from_db()
        assert run.status == TestRun.STATUS_PENDING, run.alma_release
    text = " ".join(str(m) for m in response.context["messages"])
    assert "Submitted 3 run(s)" in text


def test_the_button_appears_only_when_there_are_siblings(client, submitter):
    runs = _three(submitter)
    client.force_login(submitter)

    body = client.get(runs[0].get_absolute_url()).content.decode()
    assert "Submit all 3 runs of this machine" in body
    assert "Submit only this one" in body

    # With the others submitted, the group button has nothing to offer.
    for run in runs[1:]:
        run.listing_proposal = dict(DETAILS)
        run.save(update_fields=["listing_proposal"])
        services.submit_for_review(run, by=submitter)
    body = client.get(runs[0].get_absolute_url()).content.decode()
    assert "runs of this machine" not in body
    assert "Submit for review" in body


def test_a_blocked_member_is_named_rather_than_counted(client, submitter):
    """"One failed" is not actionable without knowing which run and why."""
    runs = _three(submitter)
    client.force_login(submitter)
    # Only the first gets details, so the others cannot be submitted.
    runs[0].listing_proposal = dict(DETAILS)
    runs[0].save(update_fields=["listing_proposal"])

    response = client.post(
        reverse("results:submit_group_for_review", args=[runs[0].uuid]), follow=True
    )

    text = " ".join(str(m) for m in response.context["messages"])
    assert "Submitted 1 run(s)" in text
    assert "was not submitted" in text
    assert "AlmaLinux 10" in text and "AlmaLinux 8" in text


def test_a_bounced_sibling_is_not_swept_in(client, submitter, reviewer):
    """It was sent back for a reason; a bulk submit must not resubmit it without
    anyone having addressed that."""
    runs = _three(submitter)
    for run in runs:
        run.listing_proposal = dict(DETAILS)
        run.save(update_fields=["listing_proposal"])
    services.submit_for_review(runs[2], by=submitter)
    services.request_run_changes(runs[2], by=reviewer, reason="Check the name.")

    client.force_login(submitter)
    client.post(reverse("results:submit_group_for_review", args=[runs[0].uuid]))

    runs[2].refresh_from_db()
    assert runs[2].status == TestRun.STATUS_NEEDS_CHANGES


# --- releases prefilled from the whole group -------------------------------------


def test_the_form_ticks_every_release_the_machine_has_a_run_for(submitter):
    """Three uploads means evidence for three releases; ticking two of them by
    hand on a form that already knows is busywork."""
    from lumina.results.forms import RunListingProposalForm

    runs = _three(submitter)      # 9.6, 10.2, 8.10

    initial = RunListingProposalForm.initial_from_run(runs[0])

    assert initial["release_9"] is True
    assert initial["release_10"] is True
    assert initial["release_8"] is True


def test_two_runs_on_one_major_tick_it_once(submitter):
    """A 9.4 pass alongside a 9.6 one says one thing: AlmaLinux 9.

    This asserted that the lower minor won, because the floor was part of the claim and taking
    the higher would have understated what was proven. Majors only now, so what is left to
    check is that the pair does not produce two answers.
    """
    from lumina.results.forms import RunListingProposalForm

    _run(submitter, "bbbbbbbb-0000-0000-0000-000000000001", "9.6")
    later = _run(submitter, "bbbbbbbb-0000-0000-0000-000000000002", "9.4")

    initial = RunListingProposalForm.initial_from_run(later)

    assert initial["release_9"] is True
    assert not any(key.startswith("release_minor") for key in initial)


def test_a_lone_run_still_ticks_only_its_own_release(submitter):
    from lumina.results.forms import RunListingProposalForm

    run = _run(submitter, "cccccccc-0000-0000-0000-000000000001", "9.6")

    initial = RunListingProposalForm.initial_from_run(run)

    assert initial["release_9"] is True
    assert "release_8" not in initial and "release_10" not in initial


# --- accepting them together ---------------------------------------------------


def _queue_three(submitter):
    """Three runs of one machine, details answered once, all awaiting review."""
    runs = _three(submitter)
    for run in runs:
        run.listing_proposal = dict(DETAILS)
        run.save(update_fields=["listing_proposal"])
        services.submit_for_review(run, by=submitter)
    return runs


def test_the_group_approve_accepts_every_queued_run(client, submitter, reviewer):
    runs = _queue_three(submitter)
    client.force_login(reviewer)

    response = client.post(
        reverse("review:run_approve_group", args=[runs[0].pk]), follow=True
    )

    for run in runs:
        run.refresh_from_db()
        assert run.status == TestRun.STATUS_APPROVED, run.alma_release
    assert System.objects.count() == 1
    assert "Approved 3 runs" in " ".join(str(m) for m in response.context["messages"])


def test_every_run_in_the_group_lands_on_the_same_listing(client, submitter,
                                                          reviewer):
    runs = _queue_three(submitter)
    client.force_login(reviewer)
    client.post(reverse("review:run_approve_group", args=[runs[0].pk]))

    system = System.objects.get()
    for run in runs:
        run.refresh_from_db()
        assert run.listing_system_id == system.pk, run.alma_release
    assert sorted(v.release.major for v in system.versions.all()) == [8, 9, 10]


def test_the_reviewer_is_shown_the_group_before_approving(client, submitter,
                                                          reviewer):
    runs = _queue_three(submitter)
    client.force_login(reviewer)

    body = client.get(reverse("review:run_detail", args=[runs[0].pk])).content.decode()

    assert "Approve all 3 runs of this machine" in body
    # Both siblings named, so the button's scope is visible rather than implied.
    for sibling in runs[1:]:
        assert str(sibling.alma_release) in body


def test_a_lone_run_offers_no_group_button(client, submitter, reviewer):
    run = _run(submitter, "cccccccc-0000-0000-0000-000000000000", "9.6")
    run.listing_proposal = dict(DETAILS)
    run.save(update_fields=["listing_proposal"])
    services.submit_for_review(run, by=submitter)
    client.force_login(reviewer)

    body = client.get(reverse("review:run_detail", args=[run.pk])).content.decode()

    assert "runs of this machine" not in body


def test_a_failing_sibling_is_left_for_its_own_review(client, submitter, reviewer):
    """The reviewer is reading this run's evidence, not that one's failures.

    Sweeping a failed run into a bulk approve would record a decision nobody
    made about results nobody looked at.
    """
    runs = _queue_three(submitter)
    broken = runs[2]
    broken.results.create(test_id="validate.memory.functional", status="fail",
                          severity="required", category="memory")
    client.force_login(reviewer)

    response = client.post(
        reverse("review:run_approve_group", args=[runs[0].pk]), follow=True
    )

    broken.refresh_from_db()
    assert broken.status == TestRun.STATUS_PENDING
    for run in runs[:2]:
        run.refresh_from_db()
        assert run.status == TestRun.STATUS_APPROVED
    text = " ".join(str(m) for m in response.context["messages"])
    assert str(broken.alma_release) in text
    assert "did not pass" in text


def test_the_run_being_viewed_is_approved_even_if_it_failed(client, submitter,
                                                            reviewer):
    """Only siblings are held back. This run is the one on screen, with its
    failures listed above the button, so approving it is a seen decision."""
    runs = _queue_three(submitter)
    runs[0].results.create(test_id="validate.memory.functional", status="fail",
                           severity="required", category="memory")
    client.force_login(reviewer)

    client.post(reverse("review:run_approve_group", args=[runs[0].pk]))

    runs[0].refresh_from_db()
    assert runs[0].status == TestRun.STATUS_APPROVED


def test_another_submitters_run_of_the_same_machine_is_untouched(client, submitter,
                                                                 reviewer):
    runs = _queue_three(submitter)
    stranger = User.objects.create_user("stranger", email="s@example.com")
    theirs = _run(stranger, "dddddddd-0000-0000-0000-000000000000", "9.6")
    theirs.listing_proposal = dict(DETAILS)
    theirs.save(update_fields=["listing_proposal"])
    services.submit_for_review(theirs, by=stranger)
    client.force_login(reviewer)

    client.post(reverse("review:run_approve_group", args=[runs[0].pk]))

    theirs.refresh_from_db()
    assert theirs.status == TestRun.STATUS_PENDING


def test_notes_are_recorded_on_every_run_in_the_group(client, submitter, reviewer):
    runs = _queue_three(submitter)
    client.force_login(reviewer)

    client.post(reverse("review:run_approve_group", args=[runs[0].pk]),
                {"notes": "Checked the SMART logs on all three."})

    for run in runs:
        run.refresh_from_db()
        assert run.reviewer_notes == "Checked the SMART logs on all three."


def test_a_decided_run_shows_no_group_button(client, submitter, reviewer):
    runs = _queue_three(submitter)
    services.approve_run(runs[0], by=reviewer)
    client.force_login(reviewer)

    body = client.get(reverse("review:run_detail", args=[runs[0].pk])).content.decode()

    assert "runs of this machine" not in body


# --- a declared major another queued run already proves --------------------------


def _run_declaring(submitter, run_id, version, majors):
    """A run on ``version`` whose proposal claims ``majors``."""
    run = _run(submitter, run_id, version)
    run.listing_proposal = {**DETAILS, **{f"release_{major}": True for major in majors}}
    run.save(update_fields=["listing_proposal"])
    return run


def test_a_declared_major_a_queued_run_proves_is_not_called_unproven(submitter):
    """Reported: the effect said "nobody has proved these" for AlmaLinux 9 while a passing run of
    the same machine sat in the queue proving exactly that. A declared major a queued sibling proves
    belongs in ``declares_queued``; one nothing proves stays in ``declares_unproven``."""
    run8 = _run_declaring(submitter, "eeeeeeee-0000-0000-0000-000000000008", "8.10", (8, 9, 10))
    run9 = _run(submitter, "eeeeeeee-0000-0000-0000-000000000009", "9.6")   # passes, proves 9
    run9.listing_proposal = dict(DETAILS)
    run9.save(update_fields=["listing_proposal"])
    for run in (run8, run9):
        services.submit_for_review(run, by=submitter)

    effect = services.proposal_effect(run8)

    by_major = {d["major"]: d for d in effect["new_declarations"]}
    assert by_major[9]["proved_in_queue"] is True, "a passing sibling runs AlmaLinux 9"
    assert by_major[10]["proved_in_queue"] is False, "nothing runs AlmaLinux 10"
    assert [d["major"] for d in effect["declares_queued"]] == [9]
    assert [d["major"] for d in effect["declares_unproven"]] == [10]


def test_a_failing_queued_sibling_does_not_back_a_declaration(submitter):
    """Only a passing run backs the declaration: ``approve_group`` leaves a failing one for its own
    review, so it must not read as proof of the major here either."""
    run8 = _run_declaring(submitter, "77777777-0000-0000-0000-000000000008", "8.10", (8, 9))
    run9 = _run(submitter, "77777777-0000-0000-0000-000000000009", "9.6")
    run9.results.create(test_id="validate.memory.functional", status="fail",
                        severity="required", category="memory")
    run9.listing_proposal = dict(DETAILS)
    run9.save(update_fields=["listing_proposal"])
    for run in (run8, run9):
        services.submit_for_review(run, by=submitter)

    effect = services.proposal_effect(run8)

    assert [d["major"] for d in effect["declares_unproven"]] == [9]
    assert effect["declares_queued"] == []


def test_the_review_page_credits_the_queued_run_not_nobody(client, submitter, reviewer):
    """The page must not tell the reviewer nobody proved AlmaLinux 9 while it lists a passing
    AlmaLinux 9 run of the same machine right below."""
    run8 = _run_declaring(submitter, "ffffffff-0000-0000-0000-000000000008", "8.10", (8, 9))
    run9 = _run(submitter, "ffffffff-0000-0000-0000-000000000009", "9.6")
    run9.listing_proposal = dict(DETAILS)
    run9.save(update_fields=["listing_proposal"])
    for run in (run8, run9):
        services.submit_for_review(run, by=submitter)
    client.force_login(reviewer)

    body = client.get(reverse("review:run_detail", args=[run8.pk])).content.decode()

    assert "A queued run of this machine proves" in body
    assert "AlmaLinux 9" in body
    # Nothing here is genuinely unproven (8 by this run, 9 by the sibling), so the "no run proving"
    # wording - and the old "nobody has proved" it replaced - must not appear.
    assert "no run proving" not in body
    assert "nobody has proved" not in body
