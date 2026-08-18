"""Settings for the self-contained dev stack (compose.yaml).

Differences from ``dev``:

- OIDC is disabled; Django's ModelBackend is the only auth backend, and
  ``lumina/urls.py`` mounts a real password login under the URL names
  mozilla_django_oidc would have published. Production never grows one:
  that branch runs only when the OIDC app is absent.
- ``LOGIN_URL`` points at that form rather than at ``/admin/login/``. The
  admin form refuses any account without ``is_staff``, so pointing here
  meant the seeded ``reviewer`` - and every submitter or vendor account
  created to exercise the submitter flows - could not log in at all.
"""
from __future__ import annotations

from lumina.settings.base import *  # noqa: F401,F403
from lumina.settings.base import INSTALLED_APPS, env, env_bool

DEBUG = env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = ["*"]
# SMTP by default, which is mailpit under compose. Env-driven because a deployed dev host has no
# mail transport, and ``send_mail`` runs inside the submit request: without an override there, a
# submission answers 500 instead of sending nothing.
EMAIL_BACKEND = env("EMAIL_BACKEND", "django.core.mail.backends.smtp.EmailBackend")

# OIDC is kept when, and only when, a realm has actually been configured. Both halves matter:
# without a client id and an authorization endpoint the OIDC app would have migrations and checks
# reaching for endpoints that are not there, and with them the password form must not exist at all.
#
# The second half is free: ``lumina/urls.py`` mounts the password login only when
# mozilla_django_oidc is absent from INSTALLED_APPS, so leaving the app installed removes that form
# without another switch. A dev host on Keycloak therefore has exactly the production login path,
# and a dev host with no realm keeps the password form it needs to be usable at all.
_oidc_configured = bool(OIDC_RP_CLIENT_ID and OIDC_OP_AUTHORIZATION_ENDPOINT)  # noqa: F405

if not _oidc_configured:
    INSTALLED_APPS = [a for a in INSTALLED_APPS if a != "mozilla_django_oidc"]
    AUTHENTICATION_BACKENDS = [
        "django.contrib.auth.backends.ModelBackend",
    ]

LOGIN_URL = "/oidc/authenticate/"
