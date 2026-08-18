"""Gated downloads of raw run evidence - bundles and their extracted artifacts.

These files carry system identity: the full ``report.json`` and the
dmidecode/lspci artifacts hold serials and the SMBIOS UUID. So they are never
public - served only to the run's submitter and to reviewers, through nginx via
``X-Accel-Redirect`` where configured. The files live under ``/media/test-runs/``,
which the vhost marks ``internal`` so nginx will not serve them except through
this handoff. Anyone not entitled gets a 404, not a 403, so which non-public runs
exist stays undisclosed - the same rule the submission-evidence handler follows.

This closes the same exposure the ``/media/test-results/`` gating fixed, on the
sibling ``test-runs/`` path. It does mean a raw bundle is no longer anonymously
downloadable; that was the price of keeping identity out of public view.
"""
from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.http import FileResponse, Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404

from lumina.results.models import RunArtifact, TestRun
from lumina.review.permissions import is_reviewer


def _may_download(user, run: TestRun) -> bool:
    return user.is_authenticated and (run.submitter_id == user.pk or is_reviewer(user))


def _serve(field_file, filename: str) -> HttpResponse:
    """Twin of ``hardware.submit_views._send_media``: X-Accel where nginx fronts
    media, else Django streams it (what the dev server and tests use)."""
    internal = getattr(settings, "LUMINA_INTERNAL_MEDIA_LOCATION", "")
    if internal:
        response = HttpResponse(status=200)
        response["X-Accel-Redirect"] = f"{internal.rstrip('/')}/{field_file.name}"
        # Left to nginx, which picks it from the extension.
        del response["Content-Type"]
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response
    return FileResponse(field_file.open("rb"), as_attachment=True, filename=filename)


def download_bundle(request: HttpRequest, uuid) -> HttpResponse:
    run = get_object_or_404(TestRun, uuid=uuid)
    if not _may_download(request.user, run) or not run.bundle:
        raise Http404
    return _serve(run.bundle, f"{run.uuid}-{Path(run.bundle.name).name}")


def download_artifact(request: HttpRequest, uuid, artifact_id: int) -> HttpResponse:
    artifact = get_object_or_404(
        RunArtifact.objects.select_related("run"), pk=artifact_id, run__uuid=uuid,
    )
    if not _may_download(request.user, artifact.run):
        raise Http404
    return _serve(artifact.file, Path(artifact.file.name).name)
