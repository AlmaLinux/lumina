"""Cross-cutting HTTP middleware."""
from __future__ import annotations

import secrets

from django.conf import settings

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


class SearchIndexingMiddleware:
    """Keep a deployment out of search results unless it is the one meant to be found.

    ``robots.txt`` asks a crawler not to *fetch* a page; it does not stop the URL being indexed
    from a link somewhere else, which surfaces as a bare title with no description. ``X-Robots-Tag``
    is the half that actually keeps it out, and it covers every response rather than only the HTML
    ones, so a PDF or a JSON endpoint is no exception.

    Off by default (see ``LUMINA_ALLOW_INDEXING``). The staging copy and production are deployed
    from the same role, so the safe default has to be the one that does nothing when nobody has
    thought about it.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self.allow = getattr(settings, "LUMINA_ALLOW_INDEXING", False)

    def __call__(self, request):
        response = self.get_response(request)
        if not self.allow:
            # setdefault in spirit: a view that has said something specific about itself keeps it.
            response.headers.setdefault("X-Robots-Tag", "noindex, nofollow")
        return response


class PrivateResponseMiddleware:
    """Stop a signed-in page being reused from the browser's cache without asking us first.

    Authenticated responses went out with no cache directives at all - measured: the dashboard
    and the review queue sent ``Vary: Cookie`` and nothing else. ``Vary`` stops a *shared* cache
    serving one person's page to another; it says nothing about the browser's own heuristic
    caching, which is free to reuse a page it was told nothing about. Reported as a review queue
    badge stuck at zero that a hard refresh fixed - a hard refresh being exactly the thing that
    bypasses that cache.

    The staleness is the visible half. The half worth more: after signing out on a shared or lab
    machine, Back could redisplay a dashboard, a run's detail, or the queue, because nothing
    required the browser to check first.

    **``no-cache``, not ``no-store``**, and the difference is deliberate. Both fix the above:
    ``no-cache`` means "keep it, but revalidate before reusing it", and after sign-out that
    revalidation returns a redirect rather than the old page. ``no-store`` additionally keeps the
    page off disk, and pays for it by disabling the back/forward cache - so Back after a mis-click
    loses a half-typed submission form. This catalog's authenticated pages hold work in progress,
    not secrets that must never touch a disk, and losing somebody's typing is the likelier harm.

    Anonymous responses are left alone. The public catalog is meant to be cached, and the SEO
    work depends on it.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if getattr(request, "user", None) is not None and request.user.is_authenticated:
            # setdefault: a view that has said something specific about itself keeps it.
            response.headers.setdefault(
                "Cache-Control", "private, no-cache, must-revalidate")
        return response
