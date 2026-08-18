"""Test settings: in-memory SQLite, locmem cache, no external services.

Using SQLite keeps unit tests hermetic. Any model feature that depends on
MariaDB-specific behavior (FULLTEXT) must be exercised via an integration
test gated on a real MariaDB, not the default pytest run.
"""
import os
import tempfile

from .base import *  # noqa: F401,F403

# On disk, not ``:memory:``, and it costs nothing measurable: the suite runs in the same 49
# seconds either way.
#
# It has to be a file because the browser tests share this configuration and a live server answers
# requests on its own thread. An in-memory SQLite database is per connection, so with one the
# reference data the migrations insert disappeared after the first test that loaded a page, and
# every later test rendered a perfectly valid page against an empty catalog: no curated CPU
# families, so no vendor claim control, so a test about that control passed by finding nothing to
# check. The request threads also raced each other closing the one shared connection, which
# segfaulted the interpreter.
# Per process, because a fixed path is a shared mutable file. Two pytest runs in two terminals, or
# two CI jobs on one machine, otherwise truncate each other's tables mid-test: the symptom is a few
# hundred unrelated errors in whichever run is unlucky, and it does not reproduce when you run
# either one on its own.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
        "TEST": {
            "NAME": os.path.join(
                tempfile.gettempdir(), f"lumina-test-{os.getpid()}.sqlite3",
            ),
        },
    }
}

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    }
}

SESSION_ENGINE = "django.contrib.sessions.backends.db"

EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# Prevent OIDC from trying to talk to the network in tests.
OIDC_RP_CLIENT_ID = "test-client"
OIDC_RP_CLIENT_SECRET = "test-secret"
OIDC_OP_AUTHORIZATION_ENDPOINT = "https://keycloak.test/auth"
OIDC_OP_TOKEN_ENDPOINT = "https://keycloak.test/token"
OIDC_OP_USER_ENDPOINT = "https://keycloak.test/userinfo"
OIDC_OP_JWKS_ENDPOINT = "https://keycloak.test/jwks"

MEDIA_ROOT = "/tmp/lumina-test-media"
