"""Bundle ingestion: happy paths and the full rejection matrix.

Ingest is the trust boundary - everything arriving here is attacker-shaped
until proven otherwise, so each guard gets a test that corrupts exactly one
property of an otherwise valid bundle.
"""
from __future__ import annotations

import io
import json
import tarfile
from decimal import Decimal

import pytest
from django.contrib.auth.models import User

from lumina.releases.models import AlmaLinuxRelease
from lumina.results import ingest
from lumina.results.models import BenchmarkResult, RunType, TestResult, TestRun
from lumina.results.tests import factories as f

pytestmark = pytest.mark.django_db


@pytest.fixture
def submitter():
    return User.objects.create_user("submitter", email="s@example.com")


def _ingest(report, artifacts=None, **kwargs):
    bundle = f.as_upload(f.build_bundle(report, artifacts))
    kwargs.setdefault("source", "api")
    return ingest.ingest_bundle(bundle_file=bundle, **kwargs)


def _decompress(buf: io.BytesIO) -> io.BytesIO:
    import zstandard

    return io.BytesIO(zstandard.ZstdDecompressor().decompress(buf.getvalue()))


# --- happy paths -------------------------------------------------------------


def test_collect_only_run_is_rejected_as_a_survey(submitter):
    # A collect-only run is a hardware-survey submission, not certification evidence,
    # so the results endpoint refuses it (and points at /api/v1/survey/). ``collect``
    # remains valid only as the first phase of a validate/benchmark run.
    with pytest.raises(ingest.InvalidReport):
        _ingest(f.make_report(run_types=["collect"]), submitter=submitter)


def test_ingest_populates_denormalized_columns(submitter):
    run = _ingest(f.make_report(run_types=["validate"]), submitter=submitter)

    assert run.schema_version == "1.1"
    assert run.bundle_sha256 and len(run.bundle_sha256) == 64
    # denormalized inventory columns feed filters and statistics
    assert run.cpu_model == "Intel(R) Xeon(R) Gold 6430"
    assert run.cpu_vendor == "GenuineIntel"
    assert run.cpu_cores == 64
    assert run.memory_mb == 524288
    # nvidia-smi's marketing name, which ``gpu_identity`` prefers over lspci's "AD102GL [L40S]".
    # The column used to be whatever the collector had already decided; it is derived now, so the
    # rule can be changed for bundles already submitted.
    assert run.gpu_model == "NVIDIA L40S"
    assert run.gpu_driver == "nvidia 570.86.15"
    assert run.system_kind == "prebuilt"
    assert run.system_vendor == "Dell Inc."
    assert run.system_product == "PowerEdge R760"
    assert run.board_vendor == "Dell Inc."
    assert run.board_model == "0M83RH"
    assert run.display_name == "Dell Inc. PowerEdge R760"
    # the verbatim inventory is retained alongside the extracted columns
    assert run.inventory["summary"]["system"]["serial"] == "ABC1234"


def test_ingest_validate_run_stores_results(submitter):
    report = f.make_report(
        run_types=["collect", "validate"],
        results=[
            f.validate_result("validate.cpu.functional"),
            f.validate_result("validate.storage.smart", category="storage"),
            f.validate_result(
                "validate.network.datapath", status="skip", severity="conditional",
                category="network",
            ),
        ],
    )
    run = _ingest(report, submitter=submitter)

    assert run.run_type == RunType.validate.value
    assert run.results.count() == 3
    smart = run.results.get(test_id="validate.storage.smart")
    assert smart.status == "pass"
    assert smart.severity == "required"
    assert smart.duration_ms == 12500
    assert run.verdict() is True
    assert run.status_counts() == {"pass": 2, "skip": 1}


def test_verdict_false_when_a_required_test_fails(submitter):
    report = f.make_report(
        run_types=["validate"],
        results=[
            f.validate_result("validate.cpu.functional"),
            f.validate_result("validate.storage.smart", status="fail", category="storage"),
        ],
    )
    assert _ingest(report, submitter=submitter).verdict() is False


def test_verdict_ignores_informational_failures(submitter):
    report = f.make_report(
        run_types=["validate"],
        results=[
            f.validate_result("validate.cpu.functional"),
            f.validate_result(
                "validate.media.audio", status="fail", severity="informational",
                category="media",
            ),
        ],
    )
    assert _ingest(report, submitter=submitter).verdict() is True


def test_ingest_benchmark_run_creates_metric_rows(submitter):
    report = f.make_report(
        run_types=["benchmark"],
        results=[
            f.benchmark_result(
                metrics=[
                    {"name": "events_per_sec", "value": 41230.5, "unit": "events/s",
                     "direction": "higher_is_better", "primary": True},
                    {"name": "latency", "value": 0.24, "unit": "ms",
                     "direction": "lower_is_better"},
                ],
                details={"threads": 32},
            )
        ],
    )
    run = _ingest(report, submitter=submitter)

    assert run.run_type == RunType.benchmark.value
    assert run.benchmarks.count() == 2
    primary = run.benchmarks.get(is_primary=True)
    assert primary.metric == "events_per_sec"
    assert primary.value == Decimal("41230.500000")
    assert primary.direction == "higher_is_better"
    assert primary.benchmark_version == "1"
    assert primary.category == "cpu"
    # benchmark_version is metadata, not run context
    assert primary.context == {"threads": 32}


def test_two_distinct_gpu_models_both_keep_their_rows(submitter):
    """An Intel iGPU + an NVIDIA dGPU each produce the same clpeak metric name. Both rows are kept
    (the second collided as a duplicate before), each tagged with its raw device and the
    server-canonicalized model."""
    from lumina.results.component_match import normalize_gpu_model

    report = f.make_report(
        run_types=["benchmark"],
        results=[
            f.benchmark_result(
                test_id="bench.gpu.clpeak", category="gpu",
                metrics=[
                    {"name": "vulkan_single_precision_compute", "value": 1100.0,
                     "unit": "GFLOPS", "direction": "higher_is_better",
                     "device": "Intel(R) UHD Graphics 630"},
                    {"name": "vulkan_single_precision_compute", "value": 89000.0,
                     "unit": "GFLOPS", "direction": "higher_is_better", "primary": True,
                     "device": "NVIDIA L40S"},
                ],
            )
        ],
    )
    run = _ingest(report, submitter=submitter)

    rows = {r.device_raw: r
            for r in run.benchmarks.filter(metric="vulkan_single_precision_compute")}
    assert set(rows) == {"Intel(R) UHD Graphics 630", "NVIDIA L40S"}
    for raw, row in rows.items():
        assert row.device_model == normalize_gpu_model(raw)
        assert row.device_ordinal == 0


def test_all_gpus_identical_cards_become_individual_rows(submitter):
    """--all-gpus: two identical cards share a device string, and the ordinal is what stops them
    collapsing into one row."""
    report = f.make_report(
        run_types=["benchmark"],
        results=[
            f.benchmark_result(
                test_id="bench.gpu.clpeak", category="gpu",
                metrics=[
                    {"name": "cuda_single_precision_compute", "value": 89000.0, "unit": "GFLOPS",
                     "direction": "higher_is_better", "device": "NVIDIA L40S", "device_ordinal": 0},
                    {"name": "cuda_single_precision_compute", "value": 88500.0, "unit": "GFLOPS",
                     "direction": "higher_is_better", "device": "NVIDIA L40S", "device_ordinal": 1},
                ],
            )
        ],
    )
    run = _ingest(report, submitter=submitter)

    rows = list(
        run.benchmarks.filter(metric="cuda_single_precision_compute").order_by("device_ordinal")
    )
    assert [r.device_ordinal for r in rows] == [0, 1]
    assert all(r.device_raw == "NVIDIA L40S" for r in rows)


def test_a_software_rasterizer_device_is_dropped_at_ingest(submitter):
    """A bundle from an older suite can carry an llvmpipe row (a CPU software Vulkan/OpenCL
    implementation clpeak benchmarked like a GPU). It is not a GPU and must not become one in the
    catalog, so it is skipped; a real card in the same bundle is kept."""
    report = f.make_report(
        run_types=["benchmark"],
        results=[
            f.benchmark_result(
                test_id="bench.gpu.clpeak", category="gpu",
                metrics=[
                    {"name": "vulkan_single_precision_compute", "value": 512.0,
                     "unit": "GFLOPS", "direction": "higher_is_better",
                     "device": "llvmpipe (LLVM 17.0.6, 256 bits)"},
                    {"name": "vulkan_single_precision_compute", "value": 6800.0,
                     "unit": "GFLOPS", "direction": "higher_is_better", "primary": True,
                     "device": "AMD Radeon Graphics (RADV PHOENIX)"},
                ],
            )
        ],
    )
    run = _ingest(report, submitter=submitter)

    rows = list(run.benchmarks.filter(metric="vulkan_single_precision_compute"))
    assert [r.device_raw for r in rows] == ["AMD Radeon Graphics (RADV PHOENIX)"]
    assert not any("llvmpipe" in r.device_model for r in rows)


def test_a_lone_software_rasterizer_leaves_no_gpu_row(submitter):
    """If the only GPU-category device in a bundle is a rasterizer, no benchmark row is stored for
    it - there is simply no GPU result, rather than a CPU one mislabeled as a card."""
    report = f.make_report(
        run_types=["benchmark"],
        results=[
            f.benchmark_result(
                test_id="bench.gpu.clpeak", category="gpu",
                metrics=[
                    {"name": "opencl_single_precision_compute", "value": 480.0,
                     "unit": "GFLOPS", "direction": "higher_is_better",
                     "device": "llvmpipe (LLVM 17.0.6, 256 bits)"},
                ],
            )
        ],
    )
    run = _ingest(report, submitter=submitter)

    assert not run.benchmarks.filter(metric="opencl_single_precision_compute").exists()


def _intel_igpu_inventory():
    """A run inventory whose only GPU is an Intel Arrow Lake iGPU (8086:7d55)."""
    inv = f.default_inventory()
    inv["summary"]["gpus"] = [
        {
            "pci": "0000:00:02.0",
            "pci_ids": {
                "vendor": "Intel Corporation [8086]",
                "device": "Arrow Lake-U [Intel Graphics] [7d55]",
            },
            "driver": "i915",
        }
    ]
    return inv


def test_a_cards_backends_get_one_pci_id_so_they_group_as_one_gpu(submitter):
    """The reported bug: Vulkan names an Intel iGPU 'Intel Graphics (ARL)' and OpenCL names the same
    card 'Intel Graphics', so the compare page listed two GPUs. Both rows are now tied to the one
    inventory card's PCI id, so they group as one whatever the backend called them - while device_raw
    and device_model keep the verbatim per-backend name for display."""
    report = f.make_report(
        run_types=["benchmark"],
        inventory=_intel_igpu_inventory(),
        results=[
            f.benchmark_result(
                test_id="bench.gpu.clpeak", category="gpu",
                metrics=[
                    {"name": "vulkan_single_precision_compute", "value": 1000.0,
                     "unit": "GFLOPS", "direction": "higher_is_better",
                     "device": "Intel Graphics (ARL)"},
                    {"name": "opencl_single_precision_compute", "value": 900.0,
                     "unit": "GFLOPS", "direction": "higher_is_better",
                     "device": "Intel Graphics"},
                ],
            )
        ],
    )
    run = _ingest(report, submitter=submitter)

    rows = list(run.benchmarks.all())
    assert {r.device_pci_id for r in rows} == {"8086:7d55"}, "both backends tie to the one card"
    assert {r.device_model for r in rows} == {"Intel Graphics (ARL)", "Intel Graphics"}


def test_the_pci_id_is_left_blank_when_the_card_is_ambiguous(submitter):
    """Two different GPUs of the same vendor: which one a clpeak name belongs to cannot be told from
    the bundle, so the row keeps a blank id and falls back to its device_model - never mis-tied to
    the wrong card."""
    inv = f.default_inventory()
    inv["summary"]["gpus"] = [
        {"pci": "0000:03:00.0",
         "pci_ids": {"vendor": "Advanced Micro Devices, Inc. [AMD/ATI] [1002]",
                     "device": "Navi 31 [Radeon RX 7900 XTX] [744c]"}, "driver": "amdgpu"},
        {"pci": "0000:0c:00.0",
         "pci_ids": {"vendor": "Advanced Micro Devices, Inc. [AMD/ATI] [1002]",
                     "device": "Navi 21 [Radeon RX 6800] [73bf]"}, "driver": "amdgpu"},
    ]
    report = f.make_report(
        run_types=["benchmark"],
        inventory=inv,
        results=[
            f.benchmark_result(
                test_id="bench.gpu.clpeak", category="gpu",
                metrics=[{"name": "vulkan_single_precision_compute", "value": 5000.0,
                          "unit": "GFLOPS", "direction": "higher_is_better",
                          "device": "AMD Radeon RX 7900 XTX (RADV NAVI31)"}],
            )
        ],
    )
    run = _ingest(report, submitter=submitter)

    row = run.benchmarks.get(metric="vulkan_single_precision_compute")
    assert row.device_pci_id == "", "two same-vendor cards: do not guess"


def test_the_cleanup_migration_removes_existing_rasterizer_rows(submitter):
    """The rows already in the database predate the ingest gate, so a migration deletes them. This
    exercises that migration's own function against a real row and a real card, confirming it drops
    only the rasterizer."""
    import importlib

    from lumina.results.models import BenchmarkResult

    # Module name starts with a digit, so it is imported by string rather than a normal import.
    _0007 = importlib.import_module(
        "lumina.results.migrations.0007_drop_software_gpu_benchmarks"
    )

    run = _ingest(f.make_report(run_types=["benchmark"], results=[f.benchmark_result()]),
                  submitter=submitter)
    keep = BenchmarkResult.objects.create(
        run=run, benchmark_id="bench.gpu.clpeak", benchmark_version="1", category="gpu",
        metric="vulkan_single_precision_compute", value=6800.0, unit="GFLOPS",
        direction="higher_is_better", device_raw="AMD Radeon Graphics (RADV PHOENIX)",
        device_model="AMD Radeon Graphics (RADV PHOENIX)",
    )
    drop = BenchmarkResult.objects.create(
        run=run, benchmark_id="bench.gpu.clpeak", benchmark_version="1", category="gpu",
        metric="vulkan_single_precision_compute", value=512.0, unit="GFLOPS",
        direction="higher_is_better", device_raw="llvmpipe (LLVM 17.0.6, 256 bits)",
        device_model="llvmpipe (LLVM 17.0.6, 256 bits)", device_ordinal=1,
    )

    from django.apps import apps
    _0007.drop_software_gpu_results(apps, None)

    assert BenchmarkResult.objects.filter(pk=keep.pk).exists()
    assert not BenchmarkResult.objects.filter(pk=drop.pk).exists()


def test_the_backfill_migration_ties_existing_rows_to_their_card(submitter):
    """Existing rows predate device_pci_id, so they carry a blank id and split by device_model. The
    0008 backfill recomputes the id from each run's inventory, healing the split without a re-run.
    Exercises the migration's own function against rows with no id set."""
    import importlib

    from lumina.results.models import BenchmarkResult

    _0008 = importlib.import_module(
        "lumina.results.migrations.0008_benchmarkresult_device_pci_id"
    )

    run = _ingest(
        f.make_report(run_types=["benchmark"], inventory=_intel_igpu_inventory(),
                      results=[f.benchmark_result()]),
        submitter=submitter,
    )
    # Two rows as an older suite would have left them: a per-backend device name, no PCI id.
    for name in ("Intel Graphics (ARL)", "Intel Graphics"):
        BenchmarkResult.objects.create(
            run=run, benchmark_id="bench.gpu.clpeak", benchmark_version="1", category="gpu",
            metric=f"{'vulkan' if '(' in name else 'opencl'}_single_precision_compute",
            value=1000.0, unit="GFLOPS", direction="higher_is_better",
            device_raw=name, device_model=name, device_pci_id="",
        )

    from django.apps import apps
    _0008.backfill_device_pci_id(apps, None)

    gpu_rows = run.benchmarks.exclude(device_raw="")
    assert {r.device_pci_id for r in gpu_rows} == {"8086:7d55"}, "both healed to the one card"


def test_a_truly_duplicate_metric_still_raises(submitter):
    """Same name, same device, same ordinal twice is a malformed bundle, not two devices."""
    report = f.make_report(
        run_types=["benchmark"],
        results=[
            f.benchmark_result(
                test_id="bench.gpu.clpeak", category="gpu",
                metrics=[
                    {"name": "cuda_single_precision_compute", "value": 1.0, "unit": "GFLOPS",
                     "device": "NVIDIA L40S", "device_ordinal": 0},
                    {"name": "cuda_single_precision_compute", "value": 2.0, "unit": "GFLOPS",
                     "device": "NVIDIA L40S", "device_ordinal": 0},
                ],
            )
        ],
    )
    with pytest.raises(ingest.InvalidReport):
        _ingest(report, submitter=submitter)


def test_a_non_gpu_metric_leaves_the_device_fields_blank(submitter):
    """CPU/disk metrics carry no device, so device_raw/device_model stay blank and ordinal 0, and
    the unique key reduces to (run, benchmark_id, metric) for them."""
    run = _ingest(
        f.make_report(run_types=["benchmark"], results=[f.benchmark_result()]),
        submitter=submitter,
    )

    row = run.benchmarks.get(metric="events_per_sec")
    assert row.device_raw == ""
    assert row.device_model == ""
    assert row.device_ordinal == 0


@pytest.mark.parametrize("bad_value", [
    pytest.param("1E20", id="beyond-the-column"),
    pytest.param("1000000000000000000", id="exactly-the-ceiling"),
    pytest.param("NaN", id="nan"),
    pytest.param("Infinity", id="infinity"),
])
def test_an_out_of_range_benchmark_value_is_a_clean_400_not_a_500(submitter, bad_value):
    """A benchmark value the column cannot hold used to reach bulk_create and surface as a 500.

    ``Decimal`` accepts NaN, Infinity, and magnitudes past the field's 18 integer digits, so the
    old "is it numeric" check passed them straight through to the database, where they became a
    DataError that is not a BundleError and so escaped as an uncaught 500. They are refused at
    ingest now, which the API layer renders as a 400.
    """
    report = f.make_report(
        run_types=["benchmark"],
        results=[f.benchmark_result(
            metrics=[{"name": "events_per_sec", "value": bad_value, "unit": "events/s",
                      "direction": "higher_is_better", "primary": True}],
        )],
    )

    with pytest.raises(ingest.InvalidReport) as caught:
        _ingest(report, submitter=submitter)

    assert "range" in str(caught.value).lower()


def test_a_large_but_storable_benchmark_value_is_accepted(submitter):
    """The bound is the column's, not tighter: a value just under 10**18 is accepted, not rejected.

    Asserted as acceptance and magnitude rather than a bit-exact value, because a Decimal at the
    column's 18-digit edge does not round-trip identically through SQLite in the test database, and
    that round-trip is not what this test is about.
    """
    # A large but ordinary benchmark number (memory bandwidth in bytes/sec is already ~1e11). Not
    # the column's exact 24-digit maximum: that edge round-trips through MariaDB but trips SQLite's
    # own read converter in the test database, which is a harness artifact, not the range this
    # guards.
    report = f.make_report(
        run_types=["benchmark"],
        results=[f.benchmark_result(
            metrics=[{"name": "events_per_sec", "value": "52682000000", "unit": "MB/s",
                      "direction": "higher_is_better", "primary": True}],
        )],
    )

    run = _ingest(report, submitter=submitter)

    assert run.benchmarks.get(is_primary=True).value == Decimal("52682000000.000000")


def test_combined_run_is_filed_as_validate(submitter):
    """A `alma-cert run` produces all three types; validation is the one that
    needs certification review, so that is where it is queued."""
    report = f.make_report(
        run_types=["collect", "validate", "benchmark"],
        results=[f.validate_result("validate.cpu.functional"), f.benchmark_result()],
    )
    run = _ingest(report, submitter=submitter)
    assert run.run_type == RunType.validate.value
    assert run.results.count() == 2
    assert run.benchmarks.count() == 1


def test_alma_release_is_resolved_from_environment(submitter):
    release = AlmaLinuxRelease.objects.get_or_create(major=9)[0]
    run = _ingest(f.make_report(version_id="9.6"), submitter=submitter)
    assert run.alma_release == release
    assert run.alma_minor == 6


def test_unknown_alma_release_is_tolerated(submitter):
    run = _ingest(f.make_report(version_id="42.0"), submitter=submitter)
    assert run.alma_release is None
    assert run.alma_minor == 0


def test_artifacts_are_stored_with_checksums(submitter):
    artifacts = {
        "artifacts/validate.cpu.functional/stress-ng.log": b"stress-ng output\n",
        "inventory/dmidecode.txt": b"BIOS Information\n",
    }
    run = _ingest(f.make_report(), artifacts, submitter=submitter)

    assert run.artifacts.count() == 2
    artifact = run.artifacts.get(bundle_path="inventory/dmidecode.txt")
    assert artifact.size == len(b"BIOS Information\n")
    assert artifact.file.read() == b"BIOS Information\n"


def test_custom_build_is_not_mistaken_for_a_vendor_system(submitter):
    """On a self-built machine, DMI mirrors the motherboard into the system
    table ("ASRock B650M PG Riptide"). That must surface as a custom build
    named by its board, not as an ASRock system model."""
    report = f.make_report(inventory=f.custom_build_inventory())
    run = _ingest(report, submitter=submitter)
    assert run.system_kind == "custom"
    assert run.board_vendor == "ASRock"
    assert run.board_model == "B650M PG Riptide"
    assert run.display_name == "Custom build: ASRock B650M PG Riptide"


def test_schema_1_0_report_still_accepted(submitter):
    """1.0 reports predate the baseboard table and the collector's kind field.

    They used to ingest as ``unknown``, because the classification lived in the collector and a
    1.0 collector never did it. Deriving it here classifies them on their merits instead: this
    report names a Dell PowerEdge R760 in its system table, which is a product, so it is a
    prebuilt - and it says so without anybody re-running anything.

    That is the whole argument for the move, on data nobody can go back and re-collect.
    """
    inventory = f.default_inventory()
    del inventory["summary"]["baseboard"]
    del inventory["summary"]["system"]["kind"]
    report = f.make_report(schema_version="1.0", inventory=inventory)
    run = _ingest(report, submitter=submitter)
    assert run.schema_version == "1.0"
    assert run.system_kind == "prebuilt"
    assert run.board_model == ""


def test_legacy_gzip_bundle_still_accepted(submitter):
    """zstd is the format alma-cert emits now, but bundles produced by older
    suite builds are gzip - both must ingest identically."""
    bundle = f.build_bundle(f.make_report(), compression="gzip")
    run = ingest.ingest_bundle(
        submitter=submitter,
        bundle_file=f.as_upload(bundle, name="bundle.tar.gz"),
        source="api",
    )
    assert run.status == TestRun.STATUS_DRAFT
    assert run.bundle.name.endswith("bundle.tar.gz")


def test_zstd_bundle_stored_with_zst_extension(submitter):
    run = _ingest(f.make_report(), submitter=submitter)
    assert run.bundle.name.endswith("bundle.tar.zst")


def test_embargo_fields_from_report(submitter):
    report = f.make_report(pre_release=True, publish_after="2026-09-01")
    run = _ingest(report, submitter=submitter)
    assert run.pre_release is True
    assert str(run.publish_requested_date) == "2026-09-01"


def test_submit_time_overrides_beat_report_values(submitter):
    """`alma-cert submit --pre-release --publish-after` must win, so an
    operator can embargo a run they already produced."""
    report = f.make_report(pre_release=False, publish_after=None)
    run = _ingest(
        report, submitter=submitter, pre_release=True, publish_after="2026-12-01"
    )
    assert run.pre_release is True
    assert str(run.publish_requested_date) == "2026-12-01"


def test_cloud_instance_target_type_is_accepted(submitter):
    """Cloud instances are a future category; the discriminator must already
    round-trip so those runs are not rejected once the suite emits them."""
    run = _ingest(f.make_report(target_type="cloud_instance"), submitter=submitter)
    assert run.target_type == "cloud_instance"


# --- rejection matrix --------------------------------------------------------


def test_oversize_bundle_rejected(submitter, settings):
    settings.LUMINA_BUNDLE_MAX_BYTES = 10
    with pytest.raises(ingest.TooLarge):
        _ingest(f.make_report(), submitter=submitter)


def test_zip_bomb_rejected(submitter, settings):
    settings.LUMINA_BUNDLE_MAX_EXTRACTED_BYTES = 100
    artifacts = {"artifacts/x/big.log": b"A" * 5000}
    with pytest.raises(ingest.BadArchive):
        _ingest(f.make_report(), artifacts, submitter=submitter)


def test_non_tar_upload_rejected(submitter):
    from django.core.files.uploadedfile import SimpleUploadedFile

    junk = SimpleUploadedFile("bundle.tar.gz", b"not a tarball", "application/gzip")
    with pytest.raises(ingest.BadArchive):
        ingest.ingest_bundle(submitter=submitter, bundle_file=junk, source="api")


def test_path_traversal_member_rejected(submitter):
    """A bundle that tries to write outside the extraction directory must be
    refused rather than silently landing files on the filesystem."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        payload = json.dumps(f.make_report()).encode()
        info = tarfile.TarInfo("report.json")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
        evil = b"pwned"
        info = tarfile.TarInfo("../../escaped.txt")
        info.size = len(evil)
        tar.addfile(info, io.BytesIO(evil))
    buf.seek(0)
    with pytest.raises(ingest.BundleError):
        ingest.ingest_bundle(
            submitter=submitter, bundle_file=f.as_upload(buf), source="api"
        )


def test_symlink_member_rejected(submitter):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        payload = json.dumps(f.make_report()).encode()
        info = tarfile.TarInfo("report.json")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
        link = tarfile.TarInfo("artifacts/passwd")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tar.addfile(link)
    buf.seek(0)
    with pytest.raises(ingest.BadArchive):
        ingest.ingest_bundle(
            submitter=submitter, bundle_file=f.as_upload(buf), source="api"
        )


def test_missing_report_rejected(submitter):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("artifacts/only.log")
        info.size = 3
        tar.addfile(info, io.BytesIO(b"abc"))
    buf.seek(0)
    with pytest.raises(ingest.InvalidReport):
        ingest.ingest_bundle(
            submitter=submitter, bundle_file=f.as_upload(buf), source="api"
        )


def test_unsupported_schema_version_rejected(submitter):
    with pytest.raises(ingest.UnsupportedSchema) as exc:
        _ingest(f.make_report(schema_version="2.0"), submitter=submitter)
    assert exc.value.code == "unsupported_schema"


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda r: r.pop("run"), id="no-run-section"),
        pytest.param(lambda r: r["run"].pop("run_id"), id="no-run-id"),
        pytest.param(lambda r: r["run"].update(run_id="not-a-uuid"), id="bad-run-id"),
        pytest.param(lambda r: r["run"].update(run_types=[]), id="empty-run-types"),
        pytest.param(lambda r: r["run"].update(run_types=["mystery"]), id="bad-run-type"),
        pytest.param(lambda r: r["run"].update(target_type="toaster"), id="bad-target"),
        pytest.param(lambda r: r.update(results="nope"), id="results-not-a-list"),
    ],
)
def test_malformed_report_rejected(submitter, mutate):
    report = f.make_report()
    mutate(report)
    with pytest.raises(ingest.BundleError):
        _ingest(report, submitter=submitter)


def test_manifest_checksum_mismatch_rejects_whole_bundle(submitter):
    """One tampered artifact invalidates everything - partial ingestion would
    leave unreviewable evidence in the database."""
    report = f.make_report()
    artifacts = {"artifacts/a/one.log": b"one", "artifacts/a/two.log": b"two"}
    buf = f.build_bundle(report, artifacts)

    # rebuild with a manifest entry deliberately pointing at the wrong hash
    with tarfile.open(fileobj=_decompress(buf), mode="r:") as tar:
        tampered = json.loads(tar.extractfile("report.json").read())
    tampered["artifact_manifest"][0]["sha256"] = "0" * 64

    out = io.BytesIO()
    payload = json.dumps(tampered).encode()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        f._add(tar, "report.json", payload)
        for path, data in sorted(artifacts.items()):
            f._add(tar, path, data)
    out.seek(0)

    with pytest.raises(ingest.ManifestMismatch):
        ingest.ingest_bundle(
            submitter=submitter, bundle_file=f.as_upload(out), source="api"
        )
    assert TestRun.objects.count() == 0
    assert TestResult.objects.count() == 0


def test_file_missing_from_bundle_rejected(submitter):
    report = f.make_report()
    report["artifact_manifest"] = [
        {"path": "artifacts/ghost.log", "sha256": "0" * 64, "size": 3}
    ]
    payload = json.dumps(report).encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        f._add(tar, "report.json", payload)
    buf.seek(0)
    with pytest.raises(ingest.ManifestMismatch):
        ingest.ingest_bundle(
            submitter=submitter, bundle_file=f.as_upload(buf), source="api"
        )


def test_unlisted_file_in_bundle_rejected(submitter):
    """Files smuggled in without a manifest entry are refused - the manifest
    must describe the bundle exactly."""
    report = f.make_report()
    listed = {"artifacts/a/one.log": b"one"}
    buf = f.build_bundle(report, listed)
    with tarfile.open(fileobj=_decompress(buf), mode="r:") as tar:
        good_report = tar.extractfile("report.json").read()

    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        f._add(tar, "report.json", good_report)
        f._add(tar, "artifacts/a/one.log", b"one")
        f._add(tar, "artifacts/a/smuggled.log", b"surprise")
    out.seek(0)
    with pytest.raises(ingest.ManifestMismatch):
        ingest.ingest_bundle(
            submitter=submitter, bundle_file=f.as_upload(out), source="api"
        )


def test_duplicate_result_id_rejected(submitter):
    report = f.make_report(
        run_types=["validate"],
        results=[
            f.validate_result("validate.cpu.functional"),
            f.validate_result("validate.cpu.functional", status="fail"),
        ],
    )
    with pytest.raises(ingest.InvalidReport):
        _ingest(report, submitter=submitter)


@pytest.mark.parametrize(
    "field,value",
    [("status", "maybe"), ("severity", "urgent")],
)
def test_invalid_result_enum_rejected(submitter, field, value):
    result = f.validate_result("validate.cpu.functional")
    result[field] = value
    with pytest.raises(ingest.InvalidReport):
        _ingest(f.make_report(run_types=["validate"], results=[result]),
                submitter=submitter)


def test_invalid_metric_direction_rejected(submitter):
    result = f.benchmark_result(
        metrics=[{"name": "x", "value": 1, "unit": "u", "direction": "sideways"}]
    )
    with pytest.raises(ingest.InvalidReport):
        _ingest(f.make_report(run_types=["benchmark"], results=[result]),
                submitter=submitter)


def test_non_numeric_metric_rejected(submitter):
    result = f.benchmark_result(
        metrics=[{"name": "x", "value": "fast", "unit": "u",
                  "direction": "higher_is_better"}]
    )
    with pytest.raises(ingest.InvalidReport):
        _ingest(f.make_report(run_types=["benchmark"], results=[result]),
                submitter=submitter)


# --- idempotency -------------------------------------------------------------


def test_identical_resubmission_reports_duplicate(submitter):
    report = f.make_report()
    bundle = f.build_bundle(report)
    first = ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(bundle), source="api"
    )
    bundle.seek(0)
    with pytest.raises(ingest.DuplicateRun) as exc:
        ingest.ingest_bundle(
            submitter=submitter, bundle_file=f.as_upload(bundle), source="api"
        )
    assert exc.value.identical is True
    assert exc.value.run.pk == first.pk
    assert TestRun.objects.count() == 1


def test_retarred_identical_report_is_still_a_duplicate(submitter):
    """`alma-cert submit` re-bundles the run dir on every invocation; archive
    bytes can differ while the report is unchanged. Identity is judged on the
    report's self-hash, so a retry stays on the friendly duplicate path.

    The second bundle is **gzip** so that its bytes genuinely differ. Building it
    with ``build_bundle`` twice produces identical archives - the helper sorts keys
    and members and zeroes tar mtimes, and zstd is deterministic - so the earlier
    version of this test matched on ``bundle_sha256`` and never reached the
    ``report_sha256`` clause it is named for. Deleting that clause from
    ``ingest.py`` left the whole suite green.

    Compression is sniffed from magic bytes rather than the filename, so a gzip
    bundle is a legitimate submission and not a second thing under test.
    """
    report = f.make_report()
    report["integrity"]["report_sha256"] = "a" * 64
    first = _ingest(report, submitter=submitter)

    zstd_bytes = f.build_bundle(report).getvalue()
    gzip_upload = f.as_upload(f.build_bundle(report, compression="gzip"))
    assert gzip_upload.read() != zstd_bytes, "the archives must differ for this to test"
    gzip_upload.seek(0)

    with pytest.raises(ingest.DuplicateRun) as exc:
        ingest.ingest_bundle(bundle_file=gzip_upload, submitter=submitter, source="api")

    assert exc.value.identical is True
    assert exc.value.run.pk == first.pk
    assert TestRun.objects.count() == 1


def test_same_run_id_different_content_is_a_conflict(submitter):
    run_id = "11111111-2222-3333-4444-555555555555"
    _ingest(f.make_report(run_id=run_id), submitter=submitter)
    with pytest.raises(ingest.DuplicateRun) as exc:
        _ingest(
            f.make_report(run_id=run_id, results=[f.validate_result("validate.x")]),
            submitter=submitter,
        )
    assert exc.value.identical is False
    assert TestRun.objects.count() == 1


def test_failed_ingest_leaves_no_partial_rows(submitter):
    report = f.make_report(
        run_types=["validate"],
        results=[
            f.validate_result("validate.cpu.functional"),
            f.validate_result("validate.bad", status="nonsense"),
        ],
    )
    with pytest.raises(ingest.InvalidReport):
        _ingest(report, submitter=submitter)
    assert TestRun.objects.count() == 0
    assert TestResult.objects.count() == 0
    assert BenchmarkResult.objects.count() == 0
