"""Submitter-facing survey views: requesting the long-lived-token capability."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render

from lumina.survey import services
from lumina.survey.forms import SurveyTokenRequestForm
from lumina.survey.models import SurveyTokenGrant, SurveyTokenRequest


@login_required
def request_token(request: HttpRequest) -> HttpResponse:
    form = SurveyTokenRequestForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            services.request_long_token(
                requester=request.user,
                justification=form.cleaned_data["justification"],
            )
        except ValueError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "Request submitted. A reviewer will follow up.")
            return redirect("survey:request_token")

    existing = (
        SurveyTokenRequest.objects.filter(requester=request.user)
        .order_by("-submitted_at").first()
    )
    return render(request, "survey/request_token.html", {
        "form": form,
        "grant": SurveyTokenGrant.objects.filter(
            user=request.user, revoked_at__isnull=True
        ).first(),
        "has_open_request": bool(
            existing and existing.status in SurveyTokenRequest.OPEN_STATUSES
        ),
        "existing": existing,
    })
