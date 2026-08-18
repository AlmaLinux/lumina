"""Submission entry point view."""
from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import (
    FileResponse,
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseRedirect,
)
from django.shortcuts import get_object_or_404, render
from django.urls import reverse

from lumina.hardware.forms import SubmissionForm
from lumina.hardware.models import Submission, TestResultAttachment
from lumina.notifications.services import emit
from lumina.review.permissions import is_reviewer
from lumina.taxonomy.models import Category


@login_required
def start(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        form = SubmissionForm(request.POST, request.FILES, user=request.user)
        if form.is_valid():
            submission = form.save()
            emit("submission.created", target=submission, actor=request.user)
            return HttpResponseRedirect(reverse("accounts:dashboard"))
    else:
        form = SubmissionForm(user=request.user)

    return render(
        request,
        "submit/start.html",
        {"form": form, "categories": Category.objects.prefetch_related("values")},
    )


@login_required
def revise(request: HttpRequest, uuid: str) -> HttpResponse:
    """Fix a submission a reviewer sent back, and put it back in the queue.

    Hardware had no such route. A reviewer could request changes, and that decision
    reached nobody: the dashboard never queried ``Submission``, no mail went out, and
    ``reviewer_notes`` appeared in no submitter-facing template. The only way to act on
    it was to submit again from scratch, which opened a *second* pending row while the
    bounced one sat in needs-changes for a reviewer to clean up by hand. Software has
    had ``software:revise`` all along; this is the same view.

    Restricted to the submitter's own needs-changes submissions. Anything else is a 404
    rather than a 403, because the set of submissions a user may revise is private and
    "wrong status" should be indistinguishable from "not yours" from outside.
    """
    submission = get_object_or_404(
        Submission.objects.select_related(
            "listing_system", "listing_system__vendor",
            "listing_component", "listing_component__vendor",
        ),
        uuid=uuid,
        submitter=request.user,
        status=Submission.STATUS_NEEDS_CHANGES,
    )
    form = SubmissionForm(
        request.POST or None, request.FILES or None,
        user=request.user, submission=submission,
    )
    if request.method == "POST" and form.is_valid():
        form.save()
        emit("submission.created", target=submission, actor=request.user)
        messages.success(
            request,
            f"{submission.listing} resubmitted. A reviewer will take another look.",
        )
        return HttpResponseRedirect(reverse("accounts:dashboard"))

    return render(
        request,
        "submit/revise.html",
        {
            "form": form,
            "submission": submission,
            "categories": Category.objects.prefetch_related("values"),
        },
    )


@login_required
def attachment(request: HttpRequest, pk: int) -> HttpResponse:
    """Serve one submission's evidence file, to people entitled to see it.

    These files used to be served straight off the public ``/media/`` alias with no
    authorization at all. Nothing was enumerable, because the path is namespaced by the
    submission's UUID4, but "unguessable" is not "protected": a URL that leaks stays
    valid forever, there is no revocation, and ``expires 7d`` invites caches to keep
    their own copies. Submission evidence is reviewer material, not catalog content.

    Visible to the submitter and to reviewers. Anyone else gets a 404 rather than a 403,
    for the same reason ``revise`` does: which submissions exist is not public, so
    "not yours" must be indistinguishable from "no such thing".
    """
    attachment = get_object_or_404(
        TestResultAttachment.objects.select_related("submission"), pk=pk,
    )
    if not (
        attachment.submission.submitter_id == request.user.pk
        or is_reviewer(request.user)
    ):
        raise Http404
    return _send_media(attachment.file, Path(attachment.file.name).name)


def _send_media(field_file, filename: str) -> HttpResponse:
    """Hand a stored file to the client, via nginx where that is configured.

    ``X-Accel-Redirect`` keeps a multi-megabyte download out of a gunicorn worker, which
    matters because the worker pool is small and an attachment can be 25 MB. Falls back
    to Django streaming it, which is what the dev server and the tests use - nothing but
    nginx understands the header, and a misconfigured deployment would otherwise serve an
    empty body rather than fail loudly.

    ``Content-Disposition: attachment`` either way, matching the header the vhost sets on
    public media: nothing uploaded here should ever render in place.
    """
    internal = getattr(settings, "LUMINA_INTERNAL_MEDIA_LOCATION", "")
    if internal:
        response = HttpResponse(status=200)
        response["X-Accel-Redirect"] = f"{internal.rstrip('/')}/{field_file.name}"
        # Left to nginx, which picks it from the extension. Django's default would
        # otherwise override the real type with text/html.
        del response["Content-Type"]
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response
    return FileResponse(field_file.open("rb"), as_attachment=True, filename=filename)
