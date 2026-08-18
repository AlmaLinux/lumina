"""record_submission writes an append-only row with a derived, stable identity."""
from __future__ import annotations

import pytest

from lumina.survey import services
from lumina.survey.models import SurveySubmission

pytestmark = pytest.mark.django_db


def _report():
    inventory = {"summary": {
        "system": {"vendor": "Dell Inc.", "product": "PowerEdge R760",
                   "uuid": "4c4c4544-0042-3510-8043-c2c04f313233", "serial": "ABC1234"},
        "baseboard": {"vendor": "Dell Inc.", "product": "0M83RH", "serial": "CN123456"},
        "cpus": [{"model": "AMD EPYC 9354 32-Core Processor", "vendor": "AuthenticAMD",
                  "sockets": 1, "cores": 32, "threads": 64}],
        "memory": {"total_bytes": 68719476736, "dimms": [{"type": "DDR5", "speed_mts": 4800}]},
        "gpus": [],
        "machine_id": "b09f3ce1e4d24a1e9f0c2a7b1c3d4e5f",
    }}
    environment = {"os": {"id": "almalinux", "version_id": "9.4", "arch": "x86_64",
                          "x86_64_level": "v3"}}
    return inventory, environment


def _record(inventory, environment):
    return services.record_submission(
        inventory=inventory, environment=environment,
        origin=SurveySubmission.ORIGIN_SURVEY,
        trust_tier=SurveySubmission.TIER_VERIFIED,
    )


def test_record_submission_creates_row_with_identity_and_verbatim_payload():
    inventory, environment = _report()
    sub = _record(inventory, environment)

    assert sub.pk
    assert sub.cpu_model == "AMD EPYC 9354"
    assert sub.system_serial == "ABC1234"          # raw identity retained
    assert sub.machine_id == "b09f3ce1e4d24a1e9f0c2a7b1c3d4e5f"
    assert sub.x86_64_level == "v3"
    assert sub.identity_source == "smbios_uuid"
    assert len(sub.identity_hash) == 64
    assert sub.inventory == inventory              # stored verbatim


def test_identity_falls_back_when_uuid_is_bogus():
    inventory, environment = _report()
    inventory["summary"]["system"]["uuid"] = "03000200-0400-0500-0006-000700080009"  # AMI default
    sub = _record(inventory, environment)
    assert sub.identity_source == "board_serial"   # fell through the bogus uuid


def test_identity_falls_back_to_machine_id_when_firmware_is_blank():
    inventory, environment = _report()
    system = inventory["summary"]["system"]
    system["uuid"] = "00000000-0000-0000-0000-000000000000"  # bogus
    system["serial"] = ""
    inventory["summary"]["baseboard"]["serial"] = ""
    sub = _record(inventory, environment)
    assert sub.identity_source == "machine_id"


def test_same_machine_hashes_identically_across_submissions():
    inventory, environment = _report()
    first = _record(inventory, environment)
    second = _record(inventory, environment)
    assert first.identity_hash == second.identity_hash != ""
