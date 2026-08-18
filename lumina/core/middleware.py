"""Cross-cutting HTTP middleware."""
from __future__ import annotations

import secrets

# The Django admin is exempt from the strict script policy. It is Unfold on top of
# django.contrib.admin, whose inline scripts do not carry our nonce (so ``'nonce-...'`` alone would
# break it) and whose Alpine.js evaluates its ``x-*`` directives with the ``Function()`` constructor
# - so it also needs ``'unsafe-eval'``. Without it every Alpine expression throws under CSP and the
# admin's sidebar, theme toggle, command palette, and modals all stop working (a stuck, uncloseable
# shortcuts modal is the visible symptom). It is staff-only and behind authentication, and the
# elimination of every third-party asset host (static/vendor) already removed the CDN-compromise
# vector for it too, so a looser script policy on this one prefix is an acceptable trade. Kept as a
# literal matching lumina/urls.py rather than reversed at request time.
_ADMIN_PREFIX = "/admin/"


class ContentSecurityPolicyMiddleware:
    """Emit a Content-Security-Policy with a per-response script nonce.

    The application serves every script, style, font, and image from its own origin (see
    static/vendor, where the former CDN and font-host assets are vendored), which is what lets
    ``script-src`` be ``'self'`` plus a nonce with no ``'unsafe-inline'`` escape hatch: the few
    inline ``<script>`` blocks carry ``{{ request.csp_nonce }}`` and there are no inline event
    handlers left for the policy to have to permit. ``style-src`` keeps ``'unsafe-inline'`` because
    inline ``style`` attributes are pervasive in the Tabler and Bootstrap components and style
    injection is a far smaller risk than script injection.

    A no-dependency middleware rather than pulling in django-csp, because the policy is short and
    adding a package needs a reason this does not yet have. Revisit that if it grows report-uri
    handling or per-view overrides.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Set before the response is built so templates can read it while they render.
        nonce = secrets.token_urlsafe(16)
        request.csp_nonce = nonce
        response = self.get_response(request)
        # setdefault: never clobber a policy a view (or nginx, for the internal media location) set
        # deliberately for itself.
        if "Content-Security-Policy" not in response.headers:
            response.headers["Content-Security-Policy"] = self._policy(request, nonce)
        return response

    @staticmethod
    def _policy(request, nonce: str) -> str:
        if request.path.startswith(_ADMIN_PREFIX):
            script_src = "script-src 'self' 'unsafe-inline' 'unsafe-eval'"
        else:
            script_src = f"script-src 'self' 'nonce-{nonce}'"
        return "; ".join([
            "default-src 'self'",
            script_src,
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data:",
            "font-src 'self'",
            "connect-src 'self'",
            "object-src 'none'",
            "base-uri 'self'",
            "frame-ancestors 'none'",
            "form-action 'self'",
        ])
