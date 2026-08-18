"""Switching what the comparison page compares, driven in a browser.

Reported: "On the 'compare hardware' page when selecting the GPUs category, only CPUs are offered."
The server was right the whole time. ``subject_options("gpu")`` returned GPU models, and a request
for ``?kind=gpu`` rendered them, so every server-side assertion passed. The kind selector sat inside
the HTMX form whose target is the results table, so choosing GPUs in a browser swapped the table and
left the model picker holding the CPU options the page was first rendered with.

That is exactly the gap this harness exists for: correct markup, wrong rendering, green suite. There
is a structural test beside the other comparison tests asserting which form the control lives in, and
it would pass against several arrangements that still do not work for a person. This drives the
control.
"""
from __future__ import annotations

import pytest

from lumina.results import services
from lumina.results.models import BenchmarkResult, TestRun
from lumina.results.tests import factories as f

pytestmark = pytest.mark.browser


@pytest.fixture
def benchmarked(submitter, reviewer, releases):
    """One published benchmark run, so both pickers have something to offer.

    The standard factory machine carries an Intel Xeon Gold 6430 and an NVIDIA L40S, which is what
    makes it usable here: one run is a subject under either kind.
    """
    from lumina.results import ingest

    report = f.make_report(
        run_types=["benchmark"],
        results=[f.benchmark_result("bench.cpu.sysbench-multi", category="cpu")],
    )
    run = ingest.ingest_bundle(
        submitter=submitter, source="api",
        bundle_file=f.as_upload(f.build_bundle(report)),
    )
    BenchmarkResult.objects.create(
        run=run, benchmark_id="bench.gpu.clpeak", benchmark_version="1", category="gpu",
        metric="vulkan_single_precision_compute", value=383.0, unit="GFLOPS",
        direction="higher_is_better", is_primary=False,
        # The GPU picker keys on the per-row device now, not the run's gpu_model, so the row must
        # name the card it ran on for the L40S to appear as a subject.
        device_raw="NVIDIA L40S", device_model="NVIDIA L40S",
    )
    services.approve_run(run, by=reviewer)
    run.refresh_from_db()
    assert run.status == TestRun.STATUS_APPROVED
    return run


def _options(page):
    return page.locator("#compare-subjects option").all_text_contents()


def test_choosing_gpus_offers_gpus(page, visit, benchmarked):
    """The reported bug, end to end. Selecting GPUs has to change the picker, not only the table."""
    visit("benchmarks:compare")
    before = _options(page)
    assert any("Gold 6430" in option for option in before), before

    page.select_option("#compare-kind", "gpu")
    page.wait_for_url("**/compare/?kind=gpu")

    after = _options(page)
    assert any("L40S" in option for option in after), after
    # And the CPU it used to offer is gone, which is the half that was broken: the picker kept its
    # old options while the table beneath it changed.
    assert not any("Gold 6430" in option for option in after), after


def test_switching_back_offers_cpus_again(page, visit, benchmarked):
    """Not a one-way door. The selector is a control, so it has to work in both directions."""
    visit("benchmarks:compare", kind="gpu")
    assert any("L40S" in option for option in _options(page))

    page.select_option("#compare-kind", "cpu")
    page.wait_for_url("**/compare/?kind=cpu")

    after = _options(page)
    assert not any("L40S" in option for option in after), after


def test_switching_kind_clears_a_selection_that_no_longer_means_anything(
    page, visit, benchmarked,
):
    """A subject key is a model string, and a CPU's is not a GPU. Carrying it across would ask the
    page to compare a CPU model under the GPU kind, which matches no runs and renders an empty
    comparison with a selection the reader did not make."""
    visit("benchmarks:compare")
    page.select_option("#compare-subjects", index=0)
    page.wait_for_selector("#compare-table")

    page.select_option("#compare-kind", "gpu")
    page.wait_for_url("**/compare/?kind=gpu")

    assert "subject=" not in page.url
    assert page.locator("#compare-subjects option[selected]").count() == 0
