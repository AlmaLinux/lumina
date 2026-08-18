"""Surfacing the CPU feature flags a bundle already carried.

The suite has collected ``inventory.summary.cpus[0].flags`` for a while and this
side did nothing with it: no extraction, no template, no API field. The flags were
present in every bundle and invisible everywhere, which is how it got reported as
"I don't see anything being reported about CPU feature flags".

Two sources, deliberately:

- the **full list** comes from the inventory, which is present even on a
  ``collect`` run with no validation results and on runs from a suite older than
  the informational test; while
- the **grouping** comes from the ``validate.cpu.flags`` result, because which
  flags are notable and how they cluster is an editorial judgement the suite
  already makes. A second copy of that table here would drift from the suite's,
  and neither copy would be wrong enough to notice.

So the display degrades to a plain list rather than to nothing when the result is
absent. See ``test_a_run_without_the_informational_result_still_lists_flags``.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User

from lumina.results import ingest, inventory_extract
from lumina.results.tests import factories as f
from lumina.results.tests.helpers import release

pytestmark = pytest.mark.django_db

FLAGS_RESULT = {
    "id": "validate.cpu.flags",
    "run_type": "validate",
    "category": "cpu",
    "severity": "informational",
    "status": "pass",
    "reason": "44 CPU feature flags reported",
    "started_at": "2026-07-27T10:05:00Z",
    "duration_s": 0.1,
    "metrics": [],
    "artifacts": [],
    "details": {
        "count": 44,
        "notable": {
            "virtualization": ["vmx", "ept"],
            "crypto_acceleration": ["aes", "vaes", "sha_ni"],
            "vector_extensions": ["avx512f", "amx_tile"],
        },
    },
}


@pytest.fixture
def submitter():
    return User.objects.create_user("flagger", password="x")


@pytest.fixture
def reviewer():
    from django.contrib.auth.models import Group

    user = User.objects.create_user("flag-reviewer", password="x")
    group, _ = Group.objects.get_or_create(name="reviewer")
    user.groups.add(group)
    return user


def _run(submitter, *, results=None, inventory=None, run_types=None):
    report = f.make_report(
        run_types=run_types or ["collect", "validate"],
        results=results if results is not None else [
            f.validate_result("validate.cpu.functional"), FLAGS_RESULT,
        ],
        inventory=inventory,
    )
    return ingest.ingest_bundle(
        submitter=submitter, source="api",
        bundle_file=f.as_upload(f.build_bundle(report)),
    )


def _page(client, run):
    return client.get(run.get_absolute_url()).content.decode()


def _flags_block(body: str) -> str:
    start = body.index('<details class="lumina-flags"')
    return body[start:body.index("</details>", start)]


# --- the inventory path -------------------------------------------------------


def test_the_fixtures_carry_flags_at_all(submitter):
    """The fixture gap that let this go unnoticed: every factory report used to
    have ``flags_virt`` and no ``flags``, so no test could have caught the flags
    being dropped."""
    run = _run(submitter)

    assert len(run.cpu_flags) > 20


def test_the_factory_list_is_sorted(submitter):
    """The suite guarantees a sorted list so two runs of one CPU are diffable. A
    fixture that quietly broke that would let unsorted-output bugs pass."""
    run = _run(submitter)

    assert run.cpu_flags == sorted(run.cpu_flags)


def test_first_cpu_reads_one_level_down_not_the_top(submitter):
    """``collect_all`` wraps the summary next to a map of raw artifact paths, so
    the CPU is at ``inventory.summary.cpus[0]``. Reading the top level returns
    nothing and looks like a machine with no CPU - the mistake that hid the flags
    on the first attempt here."""
    run = _run(submitter)

    assert inventory_extract.first_cpu(run.inventory).get("model")
    assert not (run.inventory or {}).get("cpus"), "the shallow path must stay empty"


@pytest.mark.parametrize("inventory", [
    {}, {"summary": {}}, {"summary": {"cpus": []}}, {"summary": {"cpus": [None]}},
    {"summary": {"cpus": ["not-a-dict"]}},
])
def test_a_malformed_inventory_yields_no_flags_rather_than_an_error(
    submitter, inventory
):
    run = _run(submitter, inventory=inventory)

    assert run.cpu_flags == []
    assert inventory_extract.first_cpu(run.inventory) == {}


# --- the grouping comes from the suite ----------------------------------------


def test_the_groups_come_from_the_informational_result(submitter):
    run = _run(submitter)

    assert run.cpu_flag_groups["virtualization"] == ["vmx", "ept"]
    assert run.cpu_flag_groups["crypto_acceleration"] == ["aes", "vaes", "sha_ni"]


def test_a_run_without_the_informational_result_still_lists_flags(submitter):
    """A collect-only run, or one from a suite older than the test. The display
    degrades to the plain list rather than disappearing."""
    run = _run(submitter, run_types=["collect"], results=[])

    assert run.cpu_flags, "the inventory still has them"
    assert run.cpu_flag_groups == {}


def test_empty_groups_are_dropped(submitter):
    """An empty "confidential computing" row would read as a finding rather than
    as absence."""
    result = dict(FLAGS_RESULT)
    result["details"] = {"notable": {"virtualization": ["vmx"],
                                     "confidential_computing": []}}
    run = _run(submitter, results=[result])

    assert run.cpu_flag_groups == {"virtualization": ["vmx"]}


def test_a_result_with_junk_details_does_not_break_the_page(client, submitter):
    result = dict(FLAGS_RESULT)
    result["details"] = {"notable": "not-a-dict"}
    run = _run(submitter, results=[result])
    client.force_login(submitter)

    assert run.cpu_flag_groups == {}
    assert "CPU flags" in _page(client, run)


# --- the run page -------------------------------------------------------------


def test_the_page_shows_the_flags(client, submitter):
    run = _run(submitter)
    client.force_login(submitter)

    block = _flags_block(_page(client, run))

    assert "feature flags" in block
    assert "avx512f" in block


def test_the_block_is_collapsed_by_default(client, submitter):
    """A current x86 CPU advertises 150-200 flags. Expanded, the list would be the
    longest thing on the page and push the run's actual results below the fold."""
    run = _run(submitter)
    client.force_login(submitter)

    block = _flags_block(_page(client, run))

    assert block.startswith("<details")
    assert " open" not in block.split(">")[0], "rendered already expanded"


def test_it_uses_a_native_details_element(client, submitter):
    """Not a Bootstrap collapse: this works with scripting off, and a browser's
    in-page search can expand a closed <details>, which display:none defeats."""
    run = _run(submitter)
    client.force_login(submitter)
    body = _page(client, run)

    assert '<details class="lumina-flags"' in body
    assert "<summary>" in _flags_block(body)


def test_the_group_labels_are_readable(client, submitter):
    """Data keys are snake_case; a label is not."""
    run = _run(submitter)
    client.force_login(submitter)

    block = _flags_block(_page(client, run))

    assert "Crypto acceleration" in block
    assert "crypto_acceleration" not in block


def test_it_says_it_is_informational(client, submitter):
    """The word matters: nothing passes or fails on these."""
    run = _run(submitter)
    client.force_login(submitter)

    assert "(informational)" in _flags_block(_page(client, run))


def test_the_count_is_visible_without_expanding(client, submitter):
    run = _run(submitter)
    client.force_login(submitter)

    summary = _flags_block(_page(client, run))
    summary = summary[:summary.index("</summary>")]

    assert str(len(run.cpu_flags)) in summary


def test_a_run_with_no_flags_shows_no_block(client, submitter):
    """An empty "CPU flags" row would imply the machine reported none, when in
    fact an older suite never sent any."""
    run = _run(submitter, inventory={"summary": {"cpus": [{"model": "Old Xeon"}]}})
    client.force_login(submitter)

    assert "CPU flags" not in _page(client, run)


def test_the_page_costs_no_extra_query_per_flag(client, submitter):
    """The groups come off the prefetched results, not a query per row."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    run = _run(submitter)
    client.force_login(submitter)
    with CaptureQueriesContext(connection) as ctx:
        client.get(run.get_absolute_url())

    assert len(ctx) < 40, len(ctx)


# --- the API ------------------------------------------------------------------


def test_the_detail_endpoint_exposes_the_flags(client, submitter):
    """So a consumer asking "does this machine have avx512f" does not have to know
    the answer lives at ``inventory.summary.cpus[0].flags``."""
    run = _run(submitter)
    client.force_login(submitter)

    body = client.get(f"/api/v1/results/{run.uuid}/").json()

    assert "avx512f" in body["cpu_flags"]
    assert body["cpu_flag_groups"]["virtualization"] == ["vmx", "ept"]


def test_the_list_serializer_does_not_declare_them(client, submitter):
    """150-200 flags per row would multiply a page of results several times over
    for something nobody filters a list on."""
    from lumina.api.serializers import TestRunDetailSerializer, TestRunSerializer

    assert "cpu_flags" not in TestRunSerializer.Meta.fields
    assert "cpu_flags" in TestRunDetailSerializer.Meta.fields


def test_a_public_run_in_the_list_carries_no_flags(client, submitter, reviewer):
    """The contract above, on a real list response.

    Needs an approved, published run: the earlier version of this test used a
    draft, which is not public, so the list came back empty and the assertion
    iterated nothing.
    """
    from lumina.results import services

    run = _run(submitter)
    services.approve_run(release(run), by=reviewer)

    rows = client.get("/api/v1/results/").json()["results"]

    assert rows, "no public run to check"
    assert all("cpu_flags" not in row for row in rows)
    assert any("cpu_model" in row for row in rows), "sanity: rows are runs"
