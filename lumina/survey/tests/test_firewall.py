"""The survey app stays catalog-free: no source module imports lumina.hardware.

The firewall is about data coupling - the survey shares the collector, never the
catalog. Normalizers are lifted rather than imported for exactly this reason, so
guard it with a test the way conventions worth keeping are guarded here. Only
actual ``import`` statements count; a docstring may still name the catalog.

One dependency is deliberate and passes this test because it is indirect:
``survey.devices`` imports ``results.exclusions`` to decide which GPUs are worth
counting, and that module reads ``ComponentExclusionRule``. It is allowed because it
is a *judgement* the platform makes once - a device nobody would catalogue is a device
nobody wants in the statistics - and a second copy of it in the survey would drift from
the one the review screen applies. What stays forbidden is the census reading the
catalog's *contents*: no Component, no System, no listing. That is the coupling this
test exists to prevent, and it is why the rule below matches the import rather than the
transitive reach.
"""
from __future__ import annotations

import pathlib
import re

_SURVEY_DIR = pathlib.Path(__file__).resolve().parent.parent
# Matches a module-level or deferred import of the catalog, not a mere mention.
_IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+lumina\.hardware\b", re.M)


def test_no_survey_module_imports_the_hardware_catalog():
    offenders = [
        path.name
        for path in _SURVEY_DIR.rglob("*.py")
        if "tests" not in path.parts and _IMPORT_RE.search(path.read_text())
    ]
    assert not offenders, f"survey must stay catalog-free; found a catalog import in {offenders}"
