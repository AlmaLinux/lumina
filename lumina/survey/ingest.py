"""Standalone survey bundle ingest.

A survey bundle is inventory-only: it never creates a ``TestRun`` and never
touches the certification path. It reuses the security-critical bundle
primitives from ``results.ingest`` (streaming decompression with a zip-bomb cap,
tar hardening, report load) but validates lightly - it needs an inventory, not a
run - and lands one append-only ``SurveySubmission``.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from django.conf import settings
from django.db import transaction

from lumina.results.ingest import (
    SUPPORTED_SCHEMA_VERSIONS,
    InvalidReport,
    TooLarge,
    UnsupportedSchema,
    _extract,
    _load_report,
    max_bundle_bytes,
)
from lumina.survey.models import SurveySubmission
from lumina.survey.services import record_submission


def _validate_survey_report(report: dict) -> None:
    version = report.get("schema_version")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise UnsupportedSchema(
            f"schema_version {version!r} is not supported; "
            f"this server accepts {sorted(SUPPORTED_SCHEMA_VERSIONS)}."
        )
    if not isinstance(report.get("inventory"), dict) or not report["inventory"]:
        raise InvalidReport("A survey bundle's report.json must carry a non-empty 'inventory'.")
    if not isinstance(report.get("environment"), dict):
        raise InvalidReport("A survey bundle's report.json must carry an 'environment' object.")


@transaction.atomic
def ingest_survey_bundle(*, bundle_file, trust_tier, submitter=None, token=None,
                         source_ip_hash="") -> SurveySubmission:
    """Ingest a standalone survey bundle into one append-only ``SurveySubmission``."""
    size = getattr(bundle_file, "size", None)
    if size is not None and size > max_bundle_bytes():
        raise TooLarge(f"Bundle is larger than the {max_bundle_bytes()}-byte limit.")

    tmp_root = Path(settings.MEDIA_ROOT) / "tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=tmp_root, prefix="survey-") as tmpdir:
        workdir = Path(tmpdir)
        _extract(bundle_file, workdir)
        report = _load_report(workdir)
        _validate_survey_report(report)
        return record_submission(
            inventory=report["inventory"],
            environment=report["environment"],
            origin=SurveySubmission.ORIGIN_SURVEY,
            trust_tier=trust_tier,
            submitter=submitter,
            token=token,
            source_ip_hash=source_ip_hash,
        )
