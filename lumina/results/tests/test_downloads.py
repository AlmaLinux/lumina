"""Raw run evidence (bundle + artifacts) is downloadable only by submitter and reviewers."""
from __future__ import annotations

from pathlib import Path

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


# --- the headers, which nothing covered ------------------------------------------
#
# Reported from a browser: every raw log and every evidence file failed with Firefox's "Corrupted
# Content Error". The response carried two Content-Disposition headers - the view's, with the
# filename, and a bare "attachment" the nginx internal location added on top - and Firefox rejects
# a response with conflicting duplicates of that header. curl ignores it, so the bytes looked fine
# from the command line.

_NGINX_CONF = (
    Path(__file__).resolve().parents[3]
    / "ansible/roles/lumina/templates/lumina.nginx.conf.j2"
)


def _location_block(header: str) -> str:
    """One location block from the vhost template.

    Line-based, and that is not fussiness: slicing to the next ``}`` stops at the first brace of
    the Jinja ``{{ lumina_media_root }}`` on the alias line, which cut every block short of the
    ``add_header`` lines and made this assertion pass against the very config it was written to
    reject.
    """
    lines = _NGINX_CONF.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if header in line)
    end = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == "}")
    return "\n".join(lines[start:end])


@pytest.mark.parametrize("location", ["/media/test-results/", "/media/test-runs/"])
def test_the_internal_locations_add_no_content_disposition(location):
    """The authorizing view sets one with the real filename and nginx forwards it. A second one
    here makes the response undownloadable in Firefox, and says less than the one it duplicates."""
    block = _location_block(f"location {location}")

    assert "internal;" in block, "the gating this whole handoff rests on"
    assert "add_header Content-Disposition" not in block


def test_the_public_media_location_still_forces_a_download():
    """Nothing authorizes those, so nginx is the only thing that can say it."""
    block = _location_block("location /media/ {")

    assert 'add_header Content-Disposition "attachment" always;' in block


def test_the_handoff_sends_one_content_disposition_naming_the_file(client, settings):
    """The view's half of it. Asserted on the header list rather than on ``resp["..."]``, which
    returns the first of a duplicated header and would pass either way."""
    settings.LUMINA_INTERNAL_MEDIA_LOCATION = "/media/"
    submitter = User.objects.create_user(username="hdr-sub", password="x")
    run = _run(submitter)
    artifact = run.artifacts.first()
    client.force_login(submitter)

    resp = client.get(
        reverse("results:download_artifact", args=[run.uuid, artifact.pk]))

    dispositions = [value for name, value in resp.items() if name == "Content-Disposition"]
    assert len(dispositions) == 1
    assert "dmidecode.txt" in dispositions[0]
    assert resp["X-Accel-Redirect"].startswith("/media/test-runs/")
    assert resp.content == b"", "the body must come from nginx, not Django"
