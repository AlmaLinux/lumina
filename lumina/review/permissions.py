"""Review-app permissions.

Reviewer status is determined by Django group membership, which is
ultimately driven by Keycloak groups synced on OIDC login. Admins are
implicit reviewers - we don't want to force them to be in two groups.
"""
from __future__ import annotations

from functools import wraps

from django.http import HttpRequest, HttpResponseForbidden

# Groups whose members may use the review UI.
REVIEWER_GROUPS = frozenset({"reviewer", "admin"})


def is_reviewer(user) -> bool:
    if not user.is_authenticated:
        return False
    return user.groups.filter(name__in=REVIEWER_GROUPS).exists()


def reviewer_required(view_func):
    """Decorator: 403 if the request user is not a reviewer."""

    @wraps(view_func)
    def wrapper(request: HttpRequest, *args, **kwargs):
        if not is_reviewer(request.user):
            return HttpResponseForbidden("Reviewer permission required.")
        return view_func(request, *args, **kwargs)

    return wrapper
