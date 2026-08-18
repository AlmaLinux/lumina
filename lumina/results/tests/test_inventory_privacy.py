"""System identity (SMBIOS UUID, serials) is stored but never served to the public."""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from lumina.api.serializers import TestRunDetailSerializer
from lumina.results import ingest
from lumina.results.inventory_extract import public_inventory
from lumina.results.tests import factories as f

pytestmark = pytest.mark.django_db
User = get_user_model()


def test_public_inventory_strips_identity_but_keeps_the_rest():
    inv = {"summary": {
        "system": {"vendor": "Dell", "product": "R760", "uuid": "abc", "serial": "S1"},
        "baseboard": {"vendor": "Dell", "product": "0M83RH", "serial": "B1"},
        "machine_id": "deadbeef",
    }}
    clean = public_inventory(inv)

    assert "uuid" not in clean["summary"]["system"]
    assert "serial" not in clean["summary"]["system"]
    assert "serial" not in clean["summary"]["baseboard"]
    assert "machine_id" not in clean["summary"]              # install identity stripped too
    assert clean["summary"]["system"]["product"] == "R760"   # non-identity kept
    assert inv["summary"]["system"]["serial"] == "S1"        # original untouched


def test_detail_serializer_omits_identity_while_the_stored_blob_keeps_it():
    submitter = User.objects.create_user(username="s", password="x")
    bundle = f.build_bundle(f.make_report())
    run = ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(bundle), source="test",
    )

    data = TestRunDetailSerializer(run).data
    system = data["inventory"]["summary"]["system"]
    assert "uuid" not in system and "serial" not in system
    assert "serial" not in data["inventory"]["summary"]["baseboard"]
    # Raw identity is still retained on the run - only the public view is scrubbed.
    assert run.inventory["summary"]["system"]["serial"] == "ABC1234"
