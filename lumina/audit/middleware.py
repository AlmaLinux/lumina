"""Binds request-scoped audit context (actor + client IP).

The middleware is registered in ``MIDDLEWARE`` after
``AuthenticationMiddleware`` so ``request.user`` is populated when we read
it. The matching ``clear_request()`` on exit prevents bleed-over between
concurrent tasks that share a contextvar scope at the wrong nesting.
"""
from __future__ import annotations

from django.http import HttpRequest, HttpResponse

from lumina.audit.context import bind_request, clear_request


def _client_ip(request: HttpRequest) -> str:
    # When running behind nginx the real client IP is in X-Forwarded-For.
    # The first entry is the originating client; the rest are proxy hops.
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    return request.META.get("REMOTE_ADDR", "") or ""


class AuditContextMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        user = request.user if hasattr(request, "user") else None
        actor = user if (user is not None and user.is_authenticated) else None
        bind_request(actor=actor, ip=_client_ip(request))
        try:
            return self.get_response(request)
        finally:
            clear_request()
