"""Helpers for building alma-cert result bundles in tests.

``build_bundle`` produces a real gzipped tarball with a correct manifest, so
ingest tests exercise the same code path a live submission does. Individual
tests then corrupt exactly one property to prove each guard works.
"""
from __future__ import annotations

import hashlib
import io
import json
import tarfile
import uuid as uuid_lib

SCHEMA_VERSION = "1.1"


def make_report(
    *,
    run_id: str | None = None,
    run_types: list[str] | None = None,
    results: list[dict] | None = None,
    pre_release: bool = False,
    publish_after: str | None = None,
    version_id: str = "9.6",
    schema_version: str = SCHEMA_VERSION,
    inventory: dict | None = None,
    target_type: str = "hardware",
    # ``environment.os.id``. Defaults to AlmaLinux because that is what almost
    # every test is about; pass another id (or "") to exercise the quarantine
    # path. Kept a plain string rather than a bool so a test can be specific
    # about *which* distribution, which is what the reviewer sees.
    os_id: str = "almalinux",
) -> dict:
    return {
        "schema_version": schema_version,
        "run": {
            "run_id": run_id or str(uuid_lib.uuid4()),
            "suite_version": "0.1.0",
            "suite_git_commit": "abc1234",
            "run_types": run_types or ["validate"],
            "profile": "standard",
            "target_type": target_type,
            "hostname": "sut.example",
            "started_at": "2026-07-27T10:00:00Z",
            "finished_at": "2026-07-27T10:30:00Z",
            "pre_release": pre_release,
            "publish_after": publish_after,
            "interactive_included": False,
            "resumed": False,
        },
        "environment": {
            "os": {
                "id": os_id,
                "version_id": version_id,
                "kernel": "5.14.0-503.el9.x86_64",
                "arch": "x86_64",
            },
            "selinux": "enforcing",
            "secure_boot": "enabled",
            "kernel_taint": 0,
            "tuned_profile": "throughput-performance",
            "cpu_governor": "performance",
            "smt": True,
            "mitigations": {},
            "installed_packages": [],
        },
        "inventory": inventory if inventory is not None else default_inventory(),
        "results": results or [],
        "artifact_manifest": [],
        "integrity": {"report_sha256": None},
    }


def default_inventory() -> dict:
    return {
        "summary": {
            "system": {
                "vendor": "Dell Inc.",
                "product": "PowerEdge R760",
                "serial": "ABC1234",
                "uuid": "4c4c4544-0042-3510-8043-c2c04f313233",
                "kind": "prebuilt",
                "bios": {"vendor": "Dell Inc.", "version": "2.4.4", "date": "2025-11-02"},
            },
            "baseboard": {
                "vendor": "Dell Inc.",
                "product": "0M83RH",
                "version": "A02",
                "serial": "CN123456",
            },
            "cpus": [
                {
                    "model": "Intel(R) Xeon(R) Gold 6430",
                    "vendor": "GenuineIntel",
                    "sockets": 2,
                    "cores": 64,
                    "threads": 128,
                    "max_mhz": "3400.0",
                    # A realistic Sapphire Rapids subset. Present at all because
                    # the fixtures used to carry only ``flags_virt``, so every
                    # test saw an empty flag list and nothing noticed that the
                    # server did not surface flags anywhere.
                    #
                    # ``sorted`` rather than a hand-ordered literal: the suite
                    # guarantees a sorted list (see its docs/schema.md) so two
                    # runs of one CPU are diffable, and a fixture that quietly
                    # broke that guarantee would let unsorted-output bugs pass.
                    "flags": sorted([
                        "fpu", "vme", "de", "pse", "tsc", "msr", "pae", "apic",
                        "clflush", "vmx", "ept", "aes", "vaes", "pclmulqdq",
                        "vpclmulqdq", "sha_ni", "gfni", "avx", "avx2",
                        "avx512f", "avx512bw", "avx512vl", "avx512dq",
                        "avx512_vnni", "avx512_bf16", "amx_tile", "amx_bf16",
                        "amx_int8", "ibpb", "ibrs", "ibrs_enhanced", "stibp",
                        "ssbd", "md_clear", "arch_capabilities", "constant_tsc",
                        "nonstop_tsc", "tsc_deadline_timer", "aperfmperf",
                        "rdtscp", "pcid", "invpcid", "hwp", "hwp_epp",
                    ]),
                    "flags_virt": "vmx",
                }
            ],
            "memory": {
                "total_bytes": 549755813888,
                "slots_total": 16,
                "slots_populated": 8,
                "dimms": [
                    {
                        "locator": f"DIMM {index}",
                        "bank_locator": f"P0 CHANNEL {'ABCDEFGH'[index]}",
                        "size_bytes": 68719476736,
                        "type": "DDR5",
                        "speed_mts": 4800,
                        "rated_speed_mts": 5600,
                        "rank": 2,
                        "manufacturer": "Micron Technology",
                        "part_number": "MTC40F2046S1RC56BD1",
                    }
                    for index in range(8)
                ],
            },
            "disks": [{"name": "nvme0n1", "transport": "nvme", "bytes": 1600321314816}],
            "nics": [
                # Named, because a NIC without a vendor and model cannot become a catalog
                # component - which is why they never showed up at all until the collector
                # learned to join lspci. The strings are as pci.ids writes them.
                {
                    "name": "eno1",
                    # Every name lspci gives, as it gives them. Choosing between them is the
                    # server's job (``nic_identity``), so the fixture carries the same evidence a
                    # real bundle does - including the "[id]" suffixes.
                    "pci_ids": {
                        "vendor": "Broadcom Inc. and subsidiaries [14e4]",
                        "device": "BCM57414 NetXtreme-E 10Gb/25Gb RDMA Ethernet "
                                  "Controller [16d7]",
                        "subsystem_vendor": "Broadcom Inc. and subsidiaries [14e4]",
                        # Unnamed by pci.ids, which is the common case for an onboard part and
                        # the one that produced a component called "Device".
                        "subsystem_device": "Device [4020]",
                    },
                    "pci": "0000:31:00.0",
                    "driver": "bnxt_en",
                    "driver_version": "1.10.2",
                    "speed_mbps": 25000,
                    "link": True,
                },
                # The second port of the same card. One component, two interfaces: dedup is
                # ``tie_key`` keying on the normalized model, and a fixture with one port would
                # not exercise it.
                {
                    "name": "eno2",
                    "pci_ids": {
                        "vendor": "Broadcom Inc. and subsidiaries [14e4]",
                        "device": "BCM57414 NetXtreme-E 10Gb/25Gb RDMA Ethernet "
                                  "Controller [16d7]",
                        "subsystem_vendor": "Broadcom Inc. and subsidiaries [14e4]",
                        "subsystem_device": "Device [4020]",
                    },
                    "pci": "0000:31:00.1",
                    "driver": "bnxt_en",
                    "driver_version": "1.10.2",
                    "speed_mbps": 25000,
                    "link": False,
                },
            ],
            "gpus": [
                {
                    "pci": "0000:ca:00.0",
                    # As the collector reports it: every lspci name, verbatim. Choosing among
                    # them is ``gpu_identity``'s job on the server.
                    "pci_ids": {
                        "vendor": "NVIDIA Corporation [10de]",
                        "device": "AD102GL [L40S] [26b9]",
                        "subsystem_vendor": "NVIDIA Corporation [10de]",
                        "subsystem_device": "Device [1851]",
                    },
                    "smi_name": "NVIDIA L40S",
                    "driver": "nvidia",
                    "driver_version": "570.86.15",
                    "runtime": {"cuda": "12.8"},
                    "vbios": "95.02.66.00.01",
                }
            ],
            "drivers": {"kernel": "5.14.0-503.el9.x86_64"},
        },
        "raw": {},
    }


def validate_result(
    test_id: str,
    status: str = "pass",
    severity: str = "required",
    category: str = "cpu",
    artifacts: list[str] | None = None,
    reason: str | None = None,
) -> dict:
    return {
        "id": test_id,
        "run_type": "validate",
        "category": category,
        "severity": severity,
        "status": status,
        "reason": reason,
        "started_at": "2026-07-27T10:05:00Z",
        "duration_s": 12.5,
        "metrics": [],
        "details": {},
        "artifacts": artifacts or [],
    }


def benchmark_result(
    test_id: str = "bench.cpu.sysbench-multi",
    metrics: list[dict] | None = None,
    category: str = "cpu",
    benchmark_version: str = "1",
    details: dict | None = None,
) -> dict:
    payload = {"benchmark_version": benchmark_version}
    payload.update(details or {})
    return {
        "id": test_id,
        "run_type": "benchmark",
        "category": category,
        "severity": None,
        "status": "pass",
        "reason": None,
        "started_at": "2026-07-27T10:05:00Z",
        "duration_s": 31.0,
        "metrics": metrics
        or [
            {
                "name": "events_per_sec",
                "value": 41230.5,
                "unit": "events/s",
                "direction": "higher_is_better",
                "primary": True,
            }
        ],
        "details": payload,
        "artifacts": [],
    }


def build_bundle(
    report: dict,
    artifacts: dict[str, bytes] | None = None,
    compression: str = "zstd",
) -> io.BytesIO:
    """Return an in-memory bundle whose manifest matches its contents.

    zstd is the format alma-cert emits; ``compression="gzip"`` builds a
    legacy bundle for the backward-compatibility test.
    """
    artifacts = artifacts or {"artifacts/validate.cpu.functional/stress-ng.log": b"ok\n"}

    manifest = [
        {
            "path": path,
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
        }
        for path, data in sorted(artifacts.items())
    ]
    report = json.loads(json.dumps(report))
    report["artifact_manifest"] = manifest

    payload = json.dumps(report, sort_keys=True, indent=2).encode("utf-8")
    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w") as tar:
        _add(tar, "report.json", payload)
        for path, data in sorted(artifacts.items()):
            _add(tar, path, data)

    if compression == "gzip":
        import gzip

        compressed = gzip.compress(tar_buf.getvalue())
    else:
        import zstandard

        compressed = zstandard.ZstdCompressor().compress(tar_buf.getvalue())
    buf = io.BytesIO(compressed)
    buf.seek(0)
    return buf


def _add(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name=name)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


def as_upload(buf: io.BytesIO, name: str = "bundle.tar.zst"):
    from django.core.files.uploadedfile import SimpleUploadedFile

    data = buf.getvalue()
    return SimpleUploadedFile(name, data, content_type="application/zstd")


def custom_build_inventory() -> dict:
    """Inventory as collected on a self-built machine: DMI mirrors the
    motherboard identity into the system table and the suite classifies it
    as a custom build. Values mirror a real ASRock B650M report."""
    inventory = default_inventory()
    inventory["summary"]["system"] = {
        "vendor": "ASRock",
        "product": "B650M PG Riptide",
        "serial": "Default string",
        "uuid": "02006b9c-0206-0000-0000-000000000000",
        "kind": "custom",
        "bios": {"vendor": "American Megatrends International, LLC.",
                 "version": "2.08.AS01", "date": "01/31/2024"},
    }
    inventory["summary"]["baseboard"] = {
        "vendor": "ASRock",
        "product": "B650M PG Riptide",
        "version": "1.0",
        "serial": "M80-E4001800042",
    }
    inventory["summary"]["cpus"] = [
        {"model": "AMD Ryzen 9 7950X 16-Core Processor", "vendor": "AuthenticAMD",
         "sockets": 1, "cores": 16, "threads": 32, "flags_virt": "svm"}
    ]
    return inventory
