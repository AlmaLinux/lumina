"""Dashboard workspace sections: my systems / components / runs."""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.urls import reverse

from lumina.hardware.models import System
from lumina.results import ingest, services
from lumina.results.tests import factories as f
from lumina.results.tests.helpers import release
from lumina.vendors.models import Vendor

pytestmark = pytest.mark.django_db


@pytest.fixture
def submitter():
    return User.objects.create_user("runner", password="x")


def _validate_run(submitter, **kw):
    report = f.make_report(
        run_types=["validate"],
        results=[f.validate_result("validate.cpu.functional")],
        **kw,
    )
    return ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(f.build_bundle(report)),
        source="api",
    )


def test_dashboard_lists_my_work_and_not_others(client, submitter):
    reviewer = User.objects.create_user(
        "rev", password="x", is_staff=True, is_superuser=True
    )
    from django.contrib.auth.models import Group

    group, _ = Group.objects.get_or_create(name="reviewer")
    reviewer.groups.add(group)

    mine = _validate_run(submitter)
    services.approve_run(release(mine), by=reviewer)  # ties CPU/GPU/board components too
    other_user = User.objects.create_user("other", password="x")
    dell, _ = Vendor.objects.get_or_create(name="Dell Inc.")
    System.objects.create(vendor=dell, name="Someone Elses Server",
                          created_by=other_user)

    client.force_login(submitter)
    resp = client.get(reverse("accounts:dashboard"))
    text = resp.text
    assert "My systems" in text
    assert "My components" in text
    assert "My validation runs" in text
    assert "My benchmark runs" in text
    # the run's auto-tied components appear as the submitter's work; the CPU
    # rolls up to its seeded family
    assert "Intel Xeon Scalable 4th Generation" in text
    # other people's listings do not
    assert "Someone Elses Server" not in text
    # inline filter hooks present
    assert 'data-table-filter' in text


def test_a_draft_listing_is_named_but_not_linked(client, submitter):
    """``hardware:detail`` refuses an unpublished listing, so linking one from the
    dashboard produced a 404 on the owner's own work.

    The same defect the software table had. A draft is still listed, because the
    owner needs to see it exists; it just stops pretending to be a page.
    """
    dell, _ = Vendor.objects.get_or_create(name="Dell Inc.")
    draft = System.objects.create(
        vendor=dell, name="Draft Server", created_by=submitter, published=False,
    )
    live = System.objects.create(
        vendor=dell, name="Live Server", created_by=submitter, published=True,
    )
    client.force_login(submitter)

    body = client.get(reverse("accounts:dashboard")).text

    assert "Draft Server" in body
    assert reverse("hardware:detail", args=[draft.slug]) not in body
    # Not over-broad: a published listing is still a link.
    assert reverse("hardware:detail", args=[live.slug]) in body
    # The reason it cannot be linked.
    assert client.get(reverse("hardware:detail", args=[draft.slug])).status_code == 404


def test_a_draft_component_is_named_but_not_linked(client, submitter):
    from lumina.hardware.models import Component, ComponentKind

    intel, _ = Vendor.objects.get_or_create(name="Intel")
    draft = Component.objects.create(
        vendor=intel, name="Draft NIC", kind=ComponentKind.nic.value,
        created_by=submitter, published=False,
    )
    client.force_login(submitter)

    body = client.get(reverse("accounts:dashboard")).text

    assert "Draft NIC" in body
    assert reverse("hardware:detail", args=[draft.slug]) not in body


def test_a_run_against_a_draft_system_does_not_link_it(client, submitter):
    """The runs table links its system too, and a run can be attached to a
    listing before that listing is published."""
    dell, _ = Vendor.objects.get_or_create(name="Dell Inc.")
    draft = System.objects.create(
        vendor=dell, name="Draft Host", created_by=submitter, published=False,
    )
    run = _validate_run(submitter)
    run.listing_system = draft
    run.save(update_fields=["listing_system"])
    client.force_login(submitter)

    body = client.get(reverse("accounts:dashboard")).text

    assert "Draft Host" in body
    assert reverse("hardware:detail", args=[draft.slug]) not in body


def test_dashboard_splits_benchmarks_from_validations(client, submitter):
    _validate_run(submitter)
    bench_report = f.make_report(run_types=["benchmark"],
                                 results=[f.benchmark_result()])
    ingest.ingest_bundle(
        submitter=submitter,
        bundle_file=f.as_upload(f.build_bundle(bench_report)),
        source="api",
    )
    client.force_login(submitter)
    resp = client.get(reverse("accounts:dashboard"))

    # Asserted on the context, not the headings. Both headings sit outside their
    # ``{% if %}``, so the old version passed on a database with no runs at all and
    # would not have noticed the two querysets being swapped in the view.
    validations = list(resp.context["my_validation_runs"])
    benchmarks = list(resp.context["my_benchmark_runs"])
    assert [r.run_type for r in validations] == ["validate"]
    assert [r.run_type for r in benchmarks] == ["benchmark"]
    assert "My validation runs" in resp.text and "My benchmark runs" in resp.text


# --- strings alma-cert quotes back to the submitter ---------------------------
#
# When a validation run is uploaded, alma-cert prints an ACTION REQUIRED block
# telling the submitter where to finish it, and it names these page elements
# verbatim so the instruction can be followed by eye:
#
#     https://<server>/my/
#     under "My validation runs", listed as "Awaiting submitter details"
#     with "Finish submission" in the Action column.
#
# Those strings live in the suite repository
# (``almacert/submit/client.py``: DASHBOARD_PATH, DASHBOARD_TABLE,
# _DRAFT_STATUS_LABEL, DASHBOARD_ACTION), which cannot import from here. So
# renaming any of them on this side turns a confident instruction into a wrong
# one, with nothing to notice. These tests fail instead - and the fix is to
# update the suite to match, not to loosen the assertion.


def test_the_dashboard_lives_where_alma_cert_says_it_does():
    assert reverse("accounts:dashboard") == "/my/"


def test_the_dashboard_names_the_elements_alma_cert_cites(client, submitter):
    """A freshly uploaded validation run is a draft, which is the state the
    ACTION REQUIRED block is written for."""
    from lumina.results.models import TestRun

    run = _validate_run(submitter)
    assert run.status == TestRun.STATUS_DRAFT, "not the state being described"
    client.force_login(submitter)

    body = client.get(reverse("accounts:dashboard")).content.decode()

    for quoted in (
        "My validation runs",          # the card heading
        "Awaiting submitter details",  # the draft status badge
        "Finish submission",           # the link in the Action column
        "<th>Action</th>",             # the column it sits in
    ):
        assert quoted in body, f"alma-cert tells submitters to look for {quoted!r}"


# --- how to produce a result -----------------------------------------------------------
#
# The front page tells a visitor how to run the suite. The dashboard is where somebody lands after
# signing in, and it named neither the command nor the two ways a result reaches the catalog, even
# though both of those already had pages of their own.


def _body(client, user):
    client.force_login(user)
    return client.get(reverse("accounts:dashboard")).content.decode()


def test_the_dashboard_says_how_to_run_the_suite(client, submitter):
    body = _body(client, submitter)

    assert "Run the certification suite" in body
    assert "sudo dnf -y install alma-cert &amp;&amp; sudo alma-cert run" in body


def test_it_names_both_ways_a_result_gets_here(client, submitter):
    """Which one applies is a fact about the machine, not a preference: a server with no route to
    this site cannot submit for itself. Offering only one path leaves that reader stuck."""
    body = _body(client, submitter)

    assert reverse("accounts:activate") in body
    assert reverse("results:upload") in body
    assert "alma-cert register" in body
    assert "alma-cert bundle" in body


def test_it_does_not_talk_about_needing_an_account(client, submitter):
    """The front page says so because its reader may not have one. This reader is signed in, and
    repeating it here would be noise."""
    body = " ".join(_body(client, submitter).split())

    assert "free account" not in body


def test_the_command_is_written_in_exactly_one_place(client, submitter):
    """Both pages include the same partial, so the placeholder becomes the real command once
    rather than leaving a public page and a signed-in page disagreeing about how to install it."""
    from pathlib import Path

    from django.conf import settings

    templates = Path(settings.BASE_DIR) / "templates"
    written = [
        str(path.relative_to(templates))
        for path in templates.rglob("*.html")
        if "install alma-cert" in path.read_text()
    ]

    assert written == ["core/_run_the_suite_command.html"]
