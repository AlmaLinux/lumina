"""The Content-Security-Policy header and its nonce.

The audit found no CSP in any environment, so a compromised or swapped third-party asset host would
have run arbitrary script in the lumina origin. The CDN and font hosts are gone now (static/vendor),
and this pins the header that keeps it that way: same-origin only, and inline script allowed solely
by a per-response nonce rather than a blanket 'unsafe-inline'.
"""
from __future__ import annotations

import pytest
from django.test import Client

pytestmark = pytest.mark.django_db


def _csp(response) -> str:
    assert "Content-Security-Policy" in response.headers, "no CSP header at all"
    return response.headers["Content-Security-Policy"]


def test_a_public_page_carries_a_nonce_based_script_policy():
    response = Client().get("/")
    policy = _csp(response)

    assert "default-src 'self'" in policy
    assert "object-src 'none'" in policy
    assert "frame-ancestors 'none'" in policy
    # script-src is 'self' plus a nonce, and specifically NOT 'unsafe-inline': a nonce with
    # 'unsafe-inline' present would be ignored by browsers and defeat the point.
    assert "script-src 'self' 'nonce-" in policy
    assert "'unsafe-inline'" not in policy.split("script-src", 1)[1].split(";", 1)[0]


def test_the_nonce_is_per_response():
    first = _csp(Client().get("/"))
    second = _csp(Client().get("/"))

    def nonce(policy):
        return policy.split("'nonce-", 1)[1].split("'", 1)[0]

    assert nonce(first) != nonce(second), "a reused nonce is no better than 'unsafe-inline'"


def test_the_rendered_inline_script_nonce_matches_the_header(client, django_user_model):
    """The nonce in the header has to be the one stamped on the page, or every inline script is
    blocked. Checked on a page that actually has an inline block: the dashboard."""
    from django.urls import reverse

    user = django_user_model.objects.create_user("csp-user", email="csp@example.org")
    client.force_login(user)

    response = client.get(reverse("accounts:dashboard"))
    policy = _csp(response)
    header_nonce = policy.split("'nonce-", 1)[1].split("'", 1)[0]

    assert f'<script nonce="{header_nonce}">'.encode() in response.content


def test_the_django_admin_is_not_locked_out_by_the_strict_policy():
    """Unfold's admin ships its own un-nonced inline scripts, so the strict policy would break it.
    The admin prefix gets a looser script-src instead; it is staff-only and behind auth, and the
    third-party asset vector is gone for it too."""
    response = Client().get("/admin/login/")
    script_src = _csp(response).split("script-src", 1)[1].split(";", 1)[0]

    assert "'unsafe-inline'" in script_src


def test_a_view_that_sets_its_own_policy_is_not_overridden(rf):
    """setdefault, not overwrite: the internal-media path and anything else with a deliberate
    policy of its own keep it."""
    from lumina.core.middleware import ContentSecurityPolicyMiddleware

    def view(request):
        from django.http import HttpResponse
        resp = HttpResponse("x")
        resp.headers["Content-Security-Policy"] = "default-src 'none'"
        return resp

    mw = ContentSecurityPolicyMiddleware(view)
    response = mw(rf.get("/anything/"))

    assert response.headers["Content-Security-Policy"] == "default-src 'none'"
