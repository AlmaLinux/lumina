"""Bundle ingestion - the single entry point for both submission paths.

The API endpoint and the manual web upload form both call ``ingest_bundle``,
so an offline submission is processed exactly like an online one.

Safety properties, in order of application:

1. size cap before anything is read,
2. decompression (zstd, or legacy gzip - sniffed from magic bytes) into a
   temp file with a hard cap on the decompressed size (zip-bomb guard),
3. tar extraction with ``filter="data"`` (blocks traversal, symlinks, device
   nodes),
4. schema_version allowlist,
5. every artifact's sha256 verified against the report's manifest - a single
   mismatch rejects the whole bundle rather than ingesting partial evidence,
6. idempotency on the run UUID.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import tarfile
import tempfile
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.files import File
from django.db import transaction
from django.utils import timezone

from lumina.audit.services import log_action
from lumina.core.files import hash_upload
from lumina.hardware.models import ComponentKind
from lumina.releases.models import AlmaLinuxRelease
from lumina.results import inventory_extract
from lumina.results.component_match import is_software_gpu, normalize_gpu_model
from lumina.results.models import (
    BenchmarkResult,
    MetricDirection,
    ResultStatus,
    RunArtifact,
    RunType,
    Severity,
    TargetType,
    TestResult,
    TestRun,
)

REPORT_NAME = "report.json"
SUPPORTED_SCHEMA_VERSIONS = {"1.0", "1.1"}


class BundleError(Exception):
    """Base for ingest failures. ``code`` is the machine-readable API code."""

    code = "invalid_bundle"

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


class TooLarge(BundleError):
    code = "too_large"


class BadArchive(BundleError):
    code = "bad_archive"


class UnsupportedSchema(BundleError):
    code = "unsupported_schema"


class InvalidReport(BundleError):
    code = "invalid_report"


class ManifestMismatch(BundleError):
    code = "manifest_mismatch"


class DuplicateRun(BundleError):
    """Same run UUID already ingested. ``identical`` distinguishes a benign
    retry (same bytes) from a genuine conflict."""

    code = "duplicate_run"

    def __init__(self, detail: str, *, run: TestRun, identical: bool):
        self.run = run
        self.identical = identical
        super().__init__(detail)


# BenchmarkResult.value is DecimalField(max_digits=24, decimal_places=6), so the largest magnitude
# the column can hold is just under 10**18. A value at or above this cannot be stored and is refused
# at ingest rather than allowed to become a database error on bulk_create.
_BENCHMARK_VALUE_CEILING = Decimal(10) ** 18


def max_bundle_bytes() -> int:
    return getattr(settings, "LUMINA_BUNDLE_MAX_BYTES", 256 * 1024 * 1024)


def max_extracted_bytes() -> int:
    return getattr(settings, "LUMINA_BUNDLE_MAX_EXTRACTED_BYTES", 4 * max_bundle_bytes())


@transaction.atomic
def ingest_bundle(
    *,
    submitter,
    bundle_file,
    source: str,
    pre_release: bool | None = None,
    publish_after: str | date | None = None,
    support_from_minor: int | None = None,
    submitter_notes: str = "",
) -> TestRun:
    """Validate and store a result bundle. Raises BundleError subclasses."""
    size = getattr(bundle_file, "size", None)
    if size is not None and size > max_bundle_bytes():
        raise TooLarge(
            f"Bundle is {size} bytes; the limit is {max_bundle_bytes()} bytes."
        )

    bundle_sha256 = hash_upload(bundle_file)

    tmp_root = Path(settings.MEDIA_ROOT) / "tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=tmp_root, prefix="ingest-") as tmpdir:
        workdir = Path(tmpdir)
        _extract(bundle_file, workdir)
        report = _load_report(workdir)
        _validate_report(report)
        manifest = _verify_manifest(workdir, report)

        run_uuid = report["run"]["run_id"]
        report_sha256 = str(
            (report.get("integrity") or {}).get("report_sha256") or ""
        )
        existing = TestRun.objects.filter(uuid=run_uuid).first()
        if existing is not None:
            # Re-tarring an unchanged run legitimately produces different
            # bundle bytes, so identity is judged on the report content.
            identical = existing.bundle_sha256 == bundle_sha256 or (
                bool(report_sha256) and existing.report_sha256 == report_sha256
            )
            raise DuplicateRun(
                "This run has already been submitted."
                if identical
                else "A different report with this run id already exists.",
                run=existing,
                identical=identical,
            )

        run = _create_run(
            report=report,
            submitter=submitter,
            source=source,
            bundle_file=bundle_file,
            bundle_sha256=bundle_sha256,
            pre_release=pre_release,
            publish_after=publish_after,
            support_from_minor=support_from_minor,
            submitter_notes=submitter_notes,
        )
        _create_results(run, report)
        _store_artifacts(run, workdir, manifest)

    # Deferred import: services imports models from this app's dependents.
    from lumina.results.services import auto_link_existing_system

    auto_linked = auto_link_existing_system(run)
    log_action(
        "test_run.ingest", target=run, actor=submitter,
        after={
            "uuid": str(run.uuid), "run_type": run.run_type, "source": source,
            "auto_linked_system": run.listing_system_id if auto_linked else None,
        },
    )
    return run


def peek_hostname(bundle_file) -> str:
    """Read ``run.hostname`` from a bundle without ingesting it.

    Used to check a token's host binding before doing the expensive work.
    Any problem returns an empty string so the real ingest path stays the one
    place that reports bundle errors.
    """
    try:
        tmp_root = Path(settings.MEDIA_ROOT) / "tmp"
        tmp_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=tmp_root, prefix="peek-") as tmpdir:
            workdir = Path(tmpdir)
            _extract(bundle_file, workdir)
            report = _load_report(workdir)
            return str((report.get("run") or {}).get("hostname") or "")
    except Exception:
        return ""
    finally:
        bundle_file.seek(0)


# --- steps -----------------------------------------------------------------


_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
_GZIP_MAGIC = b"\x1f\x8b"


def _decompress_with_cap(bundle_file, workdir: Path):
    """Decompress the bundle into a temp file, enforcing the extracted-size
    cap while streaming - a zip bomb dies here, not on the filesystem."""
    import zstandard

    head = bundle_file.read(4)
    bundle_file.seek(0)
    if head.startswith(_ZSTD_MAGIC):
        reader = zstandard.ZstdDecompressor().stream_reader(bundle_file)
    elif head.startswith(_GZIP_MAGIC):
        # legacy format from pre-zstd suite versions; still accepted
        reader = gzip.GzipFile(fileobj=bundle_file)
    else:
        raise BadArchive("Bundle is not zstd- or gzip-compressed.")

    tmp = tempfile.TemporaryFile(dir=workdir)
    total = 0
    try:
        while True:
            chunk = reader.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_extracted_bytes():
                raise BadArchive("Bundle expands beyond the allowed uncompressed size.")
            tmp.write(chunk)
    except (OSError, EOFError, zstandard.ZstdError) as exc:
        raise BadArchive(f"Bundle could not be decompressed: {exc}") from exc
    tmp.seek(0)
    return tmp


def _extract(bundle_file, workdir: Path) -> None:
    bundle_file.seek(0)
    tar_tmp = _decompress_with_cap(bundle_file, workdir)
    try:
        with tarfile.open(fileobj=tar_tmp, mode="r:") as tar:
            for member in tar.getmembers():
                if member.isdir():
                    continue
                if not member.isfile():
                    raise BadArchive(
                        f"Bundle contains a non-regular file: {member.name}"
                    )
            # filter="data" rejects absolute paths, "..", symlinks, and
            # special files (Python 3.12+; the default in 3.14).
            tar.extractall(path=workdir, filter="data")
    except tarfile.TarError as exc:
        raise BadArchive(f"Bundle is not a readable tar archive: {exc}") from exc
    finally:
        tar_tmp.close()
        bundle_file.seek(0)


def _load_report(workdir: Path) -> dict:
    report_path = workdir / REPORT_NAME
    if not report_path.is_file():
        raise InvalidReport("Bundle does not contain report.json.")
    try:
        with report_path.open(encoding="utf-8") as fh:
            report = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise InvalidReport(f"report.json is not valid JSON: {exc}") from exc
    if not isinstance(report, dict):
        raise InvalidReport("report.json must contain a JSON object.")
    return report


def _validate_report(report: dict) -> None:
    version = report.get("schema_version")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise UnsupportedSchema(
            f"schema_version {version!r} is not supported; "
            f"this server accepts {sorted(SUPPORTED_SCHEMA_VERSIONS)}."
        )
    for key in ("run", "environment", "inventory", "results", "artifact_manifest"):
        if key not in report:
            raise InvalidReport(f"report.json is missing the {key!r} section.")

    run = report["run"]
    if not isinstance(run, dict):
        raise InvalidReport("report.json 'run' must be an object.")
    for key in ("run_id", "suite_version", "run_types"):
        if not run.get(key):
            raise InvalidReport(f"report.json run.{key} is required.")
    try:
        import uuid as uuid_lib

        uuid_lib.UUID(str(run["run_id"]))
    except (ValueError, AttributeError, TypeError) as exc:
        raise InvalidReport("report.json run.run_id is not a valid UUID.") from exc

    run_types = run["run_types"]
    if not isinstance(run_types, list) or not run_types:
        raise InvalidReport("report.json run.run_types must be a non-empty list.")
    valid_types = {t.value for t in RunType}
    unknown = [t for t in run_types if t not in valid_types]
    if unknown:
        raise InvalidReport(f"Unknown run type(s): {', '.join(map(str, unknown))}.")

    if run.get("target_type", TargetType.hardware.value) not in {
        t.value for t in TargetType
    }:
        raise InvalidReport(f"Unknown target_type {run.get('target_type')!r}.")

    # A scope nobody can interpret would be a claim of unknown size, so it is refused at the door
    # rather than stored and guessed at later. Absent is the ordinary case and means the whole
    # machine, which is what every bundle written before this field existed says.
    scope = run.get("claim_scope") or []
    if not isinstance(scope, list):
        raise InvalidReport("report.json run.claim_scope must be a list of component kinds.")
    known_kinds = {k.value for k in ComponentKind}
    unknown_kinds = [k for k in scope if k not in known_kinds]
    if unknown_kinds:
        raise InvalidReport(
            f"Unknown claim_scope component kind(s): {', '.join(map(str, unknown_kinds))}."
        )

    if not isinstance(report["results"], list):
        raise InvalidReport("report.json 'results' must be a list.")
    if not isinstance(report["artifact_manifest"], list):
        raise InvalidReport("report.json 'artifact_manifest' must be a list.")


def _verify_manifest(workdir: Path, report: dict) -> list[dict]:
    manifest = report["artifact_manifest"]
    listed: set[str] = set()
    for entry in manifest:
        if not isinstance(entry, dict) or "path" not in entry or "sha256" not in entry:
            raise InvalidReport("Each artifact_manifest entry needs 'path' and 'sha256'.")
        rel = entry["path"]
        if rel.startswith("/") or ".." in Path(rel).parts:
            raise ManifestMismatch(f"Manifest path escapes the bundle: {rel}")
        path = workdir / rel
        if not path.is_file():
            raise ManifestMismatch(f"Manifest lists a file missing from the bundle: {rel}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != entry["sha256"]:
            raise ManifestMismatch(f"Checksum mismatch for {rel}.")
        listed.add(rel)

    on_disk = {
        str(p.relative_to(workdir))
        for p in workdir.rglob("*")
        if p.is_file() and p.name not in (REPORT_NAME, "SHA256SUMS")
    }
    extra = on_disk - listed
    if extra:
        raise ManifestMismatch(
            "Bundle contains files absent from the manifest: "
            + ", ".join(sorted(extra)[:5])
        )
    return manifest


def _initial_status(run_type: str, environment: dict) -> str:
    """Validation runs need submitter-supplied listing detail before review;
    benchmarks and bare inventory do not.

    A run that was not performed on AlmaLinux is quarantined instead, whatever its
    type. It is kept rather than refused so that an attempted submission is
    visible to reviewers, but it starts outside ``OPEN_STATUSES``, so nothing can
    approve it, publish it, or turn it into an attestation until a reviewer
    releases it deliberately.

    Benchmarks are included on purpose. The leaderboards compare AlmaLinux
    machines; a Rocky result on them is not a like-for-like comparison, and it
    would be invisible as anything else once ranked beside the rest.
    """
    if not inventory_extract.is_almalinux(environment):
        return TestRun.STATUS_QUARANTINED
    return normal_initial_status(run_type)


def normal_initial_status(run_type: str) -> str:
    """Where a run starts once the OS gate is satisfied.

    Split out because ``release_from_quarantine`` needs it and cannot call
    ``_initial_status``: the environment it would pass still says Rocky, so the
    run would be quarantined again by the very function meant to let it out.
    """
    if run_type == RunType.validate.value:
        return TestRun.STATUS_DRAFT
    return TestRun.STATUS_PENDING


def _create_run(
    *,
    report: dict,
    submitter,
    source: str,
    bundle_file,
    bundle_sha256: str,
    pre_release: bool | None,
    publish_after: str | date | None,
    support_from_minor: int | None,
    submitter_notes: str,
) -> TestRun:
    run_meta = report["run"]
    environment = report.get("environment") or {}
    inventory = report.get("inventory") or {}

    major, minor = inventory_extract.parse_release(environment)
    release = AlmaLinuxRelease.objects.filter(major=major).first() if major else None

    effective_pre_release = (
        bool(run_meta.get("pre_release")) if pre_release is None else bool(pre_release)
    )
    effective_publish = _parse_date(
        run_meta.get("publish_after") if publish_after is None else publish_after
    )
    # The timing gate, from the ``--support-from-minor`` flag. Carried both inside the report's
    # run metadata and as a submit field, so ``alma-cert submit`` can correct a value the run
    # was started without; the explicit field wins, as it does for the two above.
    #
    # Honoured only for a run on AlmaLinux Kitten. A pass on a shipped release has nothing to
    # wait for, so a value there would put a disclaimer on a claim the run just proved - and a
    # flag typed on the wrong run should not be able to do that. Reviewers can still set one by
    # hand if a run needs it.
    effective_from_minor = (
        run_meta.get("support_from_minor") if support_from_minor is None
        else support_from_minor
    )
    if not _is_prerelease_os(environment):
        effective_from_minor = None

    run_type = _primary_run_type(run_meta["run_types"])
    run = TestRun(
        uuid=run_meta["run_id"],
        run_type=run_type,
        status=_initial_status(run_type, environment),
        host_os_id=inventory_extract.host_os_id(environment)[:32],
        target_type=run_meta.get("target_type", TargetType.hardware.value),
        # Deduplicated and ordered, so two reports claiming the same thing in a different order
        # produce the same stored value and the same rendered sentence.
        claim_scope=sorted(set(run_meta.get("claim_scope") or [])),
        schema_version=report["schema_version"],
        suite_version=str(run_meta.get("suite_version", ""))[:40],
        suite_git_commit=str(run_meta.get("suite_git_commit") or "")[:40],
        submitter=submitter,
        source=source,
        alma_release=release,
        alma_minor=minor,
        inventory=inventory,
        environment=environment,
        bundle_sha256=bundle_sha256,
        report_sha256=str(
            (report.get("integrity") or {}).get("report_sha256") or ""
        )[:64],
        bundle_size=getattr(bundle_file, "size", 0) or 0,
        pre_release=effective_pre_release,
        publish_requested_date=effective_publish,
        available_from_minor=_parse_minor(effective_from_minor),
        submitter_notes=submitter_notes,
        started_at=_parse_dt(run_meta.get("started_at")),
        finished_at=_parse_dt(run_meta.get("finished_at")),
        **inventory_extract.extract(inventory),
    )
    bundle_file.seek(0)
    original = str(getattr(bundle_file, "name", "") or "")
    stored_name = "bundle.tar.gz" if original.endswith(".tar.gz") else "bundle.tar.zst"
    run.bundle.save(stored_name, File(bundle_file), save=False)
    run.save()
    return run


def _primary_run_type(run_types: list) -> str:
    """A combined run is filed under its most reviewable type: validation
    outranks benchmarking, which outranks bare collection."""
    for candidate in (RunType.validate, RunType.benchmark, RunType.collect):
        if candidate.value in run_types:
            return candidate.value
    return RunType.collect.value


def _create_results(run: TestRun, report: dict) -> None:
    results: list[TestResult] = []
    benchmarks: list[BenchmarkResult] = []
    valid_status = {s.value for s in ResultStatus}
    valid_severity = {s.value for s in Severity}
    valid_direction = {d.value for d in MetricDirection}
    seen_tests: set[str] = set()
    seen_metrics: set[tuple[str, str, str, int]] = set()

    for entry in report["results"]:
        if not isinstance(entry, dict) or not entry.get("id"):
            raise InvalidReport("Each result needs an 'id'.")
        test_id = str(entry["id"])[:120]
        if test_id in seen_tests:
            raise InvalidReport(f"Duplicate result id in report: {test_id}")
        seen_tests.add(test_id)

        status = entry.get("status")
        if status not in valid_status:
            raise InvalidReport(f"{test_id}: invalid status {status!r}.")
        severity = entry.get("severity") or ""
        if severity and severity not in valid_severity:
            raise InvalidReport(f"{test_id}: invalid severity {severity!r}.")

        duration = entry.get("duration_s")
        results.append(
            TestResult(
                run=run,
                test_id=test_id,
                category=str(entry.get("category") or "")[:80],
                severity=severity,
                status=status,
                reason=str(entry.get("reason") or ""),
                duration_ms=int(duration * 1000) if isinstance(duration, (int, float)) else None,
                details=entry.get("details") or {},
            )
        )

        if entry.get("run_type") != RunType.benchmark.value:
            continue
        details = entry.get("details") or {}
        benchmark_version = str(details.get("benchmark_version") or "1")[:40]
        for metric in entry.get("metrics") or []:
            if not isinstance(metric, dict) or not metric.get("name"):
                raise InvalidReport(f"{test_id}: each metric needs a 'name'.")
            metric_name = str(metric["name"])[:80]
            # The device the figure came from, when a benchmark ran on more than one of the same
            # kind (clpeak on each GPU). device_raw is ground truth; device_ordinal (0 unless
            # --all-gpus splits identical cards) is non-null so the dedupe key and the DB unique
            # constraint agree. Guard against bool, which is an int subclass.
            device_raw = str(metric.get("device") or "")[:200]
            if device_raw and is_software_gpu(device_raw):
                # A software rasterizer (llvmpipe and friends) is a CPU implementation a benchmark
                # can enumerate and measure like a GPU. It is not one, so it is not stored as a
                # benchmark result - it would otherwise show up as a GPU in the catalog. Current
                # suites drop it at the source; this catches a bundle from an older one. The raw
                # bundle stays as the submission's evidence regardless.
                continue
            raw_ordinal = metric.get("device_ordinal")
            device_ordinal = (
                raw_ordinal
                if isinstance(raw_ordinal, int) and not isinstance(raw_ordinal, bool)
                else 0
            )
            key = (test_id, metric_name, device_raw, device_ordinal)
            if key in seen_metrics:
                raise InvalidReport(
                    f"{test_id}: duplicate metric {metric_name} for device "
                    f"{device_raw!r} ordinal {device_ordinal}."
                )
            seen_metrics.add(key)
            direction = metric.get("direction", MetricDirection.INFO.value)
            if direction not in valid_direction:
                raise InvalidReport(
                    f"{test_id}.{metric_name}: invalid direction {direction!r}."
                )
            try:
                value = Decimal(str(metric["value"]))
            except (KeyError, InvalidOperation, TypeError) as exc:
                raise InvalidReport(
                    f"{test_id}.{metric_name}: value is not numeric."
                ) from exc
            # ``Decimal`` accepts NaN and Infinity, and a value larger than the column holds. Both
            # reached bulk_create and turned an attacker-supplied number into a 500: NaN/Inf as a
            # database error, and an over-24-digit value as a DataError, neither of them a
            # BundleError the API layer catches. Rejected here as a 400 instead. The bound is the
            # field's own: max_digits=24, decimal_places=6 leaves 18 integer digits. Checked before
            # any quantize, because quantizing a wild magnitude can overflow the Decimal context
            # and raise in its own right.
            if not value.is_finite() or value.copy_abs() >= _BENCHMARK_VALUE_CEILING:
                raise InvalidReport(
                    f"{test_id}.{metric_name}: value is out of the supported range."
                )
            # Canonicalize server-side (lumina owns this): device_raw is preserved verbatim, and
            # device_model is derived so a future change to the mapping can be reapplied to old
            # rows. Reuses the one GPU normalizer rather than a second copy that could drift.
            device_model = normalize_gpu_model(device_raw)[:200] if device_raw else ""
            benchmarks.append(
                BenchmarkResult(
                    run=run,
                    benchmark_id=test_id,
                    benchmark_version=benchmark_version,
                    category=str(entry.get("category") or "")[:80],
                    metric=metric_name,
                    value=value,
                    unit=str(metric.get("unit") or "")[:40],
                    direction=direction,
                    is_primary=bool(metric.get("primary")),
                    device_raw=device_raw,
                    device_model=device_model,
                    device_ordinal=device_ordinal,
                    context={
                        k: v for k, v in details.items() if k != "benchmark_version"
                    },
                )
            )

    TestResult.objects.bulk_create(results)
    BenchmarkResult.objects.bulk_create(benchmarks)


def _store_artifacts(run: TestRun, workdir: Path, manifest: list[dict]) -> None:
    for entry in manifest:
        rel = entry["path"]
        path = workdir / rel
        artifact = RunArtifact(
            run=run,
            bundle_path=rel,
            sha256=entry["sha256"],
            size=entry.get("size") or path.stat().st_size,
        )
        with path.open("rb") as fh:
            artifact.file.save(rel.replace(os.sep, "__"), File(fh), save=False)
        artifact.save()


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.utc)
    return parsed


def _is_prerelease_os(environment: dict) -> bool:
    """Whether this report says it ran on AlmaLinux Kitten.

    The same reading ``TestRun.ran_on_prerelease_os`` does, applied before the row exists.
    Kitten names itself only in ``PRETTY_NAME``; every other field matches the stable release
    of the same major.
    """
    pretty = ((environment or {}).get("os") or {}).get("pretty_name") or ""
    return TestRun.PRERELEASE_OS_MARKER in pretty.lower()


def _parse_minor(value) -> int | None:
    """A minor from the wire, or None. Junk is dropped rather than raising: the gate is a
    courtesy to the reader, and refusing an upload over it would lose the whole run."""
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise InvalidReport(f"publish_after {value!r} is not a YYYY-MM-DD date.") from None
