"""Plain helpers for the browser tests. The pytest fixtures live in ``conftest.py``."""
from __future__ import annotations

from lumina.results import ingest
from lumina.results.tests import factories as f


def make_run(submitter, **report_kw):
    """An ingested validate run of the standard Dell machine the factories describe."""
    return ingest.ingest_bundle(
        submitter=submitter, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"],
            results=[f.validate_result("validate.cpu.functional")],
            **report_kw,
        ))),
    )
