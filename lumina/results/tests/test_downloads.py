"""Raw run evidence (bundle + artifacts) is downloadable only by submitter and reviewers."""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.urls import reverse

from lumina.results import ingest
from lumina.results.tests import factories as f

pytestmark = pytest.mark.django_db
User = get_user_model()


def _run(submitter):
    bundle = f.build_bundle(
        f.make_report(),
        artifacts={"artifacts/dmidecode.txt": b"UUID: 4c...\nSerial Number: ABC1234\n"},
    )
    return ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(bundle), source="test",
    )


def test_bundle_download_is_gated(client):
    submitter = User.objects.create_user(username="sub", password="x")
    run = _run(submitter)
    url = reverse("results:download_bundle", args=[run.uuid])

    # Anonymous: 404, not 403 - which non-public runs exist is not disclosed.
    assert client.get(url).status_code == 404
    # Submitter: served.
    client.force_login(submitter)
    assert client.get(url).status_code == 200


def test_artifact_download_gated_to_submitter_and_reviewers(client):
    submitter = User.objects.create_user(username="sub", password="x")
    other = User.objects.create_user(username="other", password="x")
    reviewer = User.objects.create_user(username="rev", password="x")
    reviewer.groups.add(Group.objects.get_or_create(name="reviewer")[0])

    run = _run(submitter)
    artifact = run.artifacts.first()
    url = reverse("results:download_artifact", args=[run.uuid, artifact.pk])

    client.force_login(other)
    assert client.get(url).status_code == 404      # entitled to nothing here
    client.force_login(submitter)
    assert client.get(url).status_code == 200
    client.force_login(reviewer)
    assert client.get(url).status_code == 200
