"""Base Django settings shared by all environments.

Environment-specific settings (dev/test/prod) import from this module and
override as needed. Secrets come from environment variables; see .env.example.
"""
from __future__ import annotations

import os
from pathlib import Path

from django.contrib.messages import constants as django_messages

BASE_DIR = Path(__file__).resolve().parent.parent.parent


def env(key: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.environ.get(key, default)
    if required and value is None:
        raise RuntimeError(f"Missing required environment variable: {key}")
    return value  # type: ignore[return-value]


def env_bool(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_list(key: str, default: list[str] | None = None) -> list[str]:
    raw = os.environ.get(key)
    if not raw:
        return default or []
    return [item.strip() for item in raw.split(",") if item.strip()]


def env_map(key: str, default: dict[str, str] | None = None) -> dict[str, str]:
    """A ``key=value,key=value`` environment variable as a dict.

    Returns the default when the variable is unset or empty, so "not configured" and "configured
    empty" stay distinguishable - for the group map that difference is the difference between
    shipping working defaults and deliberately granting nothing.
    """
    raw = os.environ.get(key)
    if raw is None or not raw.strip():
        return dict(default or {})
    pairs = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise RuntimeError(
                f"{key}: expected comma-separated key=value pairs, got {item!r}"
            )
        name, _, value = item.partition("=")
        name, value = name.strip(), value.strip()
        if not name or not value:
            raise RuntimeError(f"{key}: neither side of a pair may be empty, got {item!r}")
        pairs[name] = value
    return pairs


SECRET_KEY = env("DJANGO_SECRET_KEY", "insecure-default-override-me")
DEBUG = env_bool("DJANGO_DEBUG", False)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", ["localhost", "127.0.0.1"])

INSTALLED_APPS = [
    # Unfold must precede django.contrib.admin to swap in its theme.
    "unfold",
    "unfold.contrib.filters",
    "unfold.contrib.forms",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third-party.
    "django_htmx",
    "django_filters",
    "rest_framework",
    "mozilla_django_oidc",
    # Local apps.
    "lumina.core",
    "lumina.accounts",
    "lumina.vendors",
    "lumina.taxonomy",
    "lumina.releases",
    "lumina.hardware",
    "lumina.software",
    "lumina.results",
    "lumina.audit",
    "lumina.review",
    "lumina.notifications",
    "lumina.survey",
    "lumina.api",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Sets request.csp_nonce and the Content-Security-Policy header. Early, so the nonce is
    # available to every view and template that renders below it.
    "lumina.core.middleware.ContentSecurityPolicyMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
    "lumina.audit.middleware.AuditContextMiddleware",
]

ROOT_URLCONF = "lumina.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# Django's default tag for an error message is the literal string "error", and both base
# templates build their class as ``alert alert-{{ message.tags }}``. Neither Bootstrap 5 nor
# Tabler defines ``.alert-error``: it is ``.alert-danger``. So every ``messages.error`` in the
# application rendered as unstyled body text with no red, no border, and no icon, which is the
# opposite of what an error needs to do, and it did it identically on both bases so nothing
# looked out of place. ``debug`` has the same problem and the same fix.
MESSAGE_TAGS = {
    django_messages.DEBUG: "secondary",
    django_messages.ERROR: "danger",
}

WSGI_APPLICATION = "lumina.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.mysql",
        "NAME": env("DB_NAME", "lumina"),
        "USER": env("DB_USER", "lumina"),
        "PASSWORD": env("DB_PASSWORD", ""),
        "HOST": env("DB_HOST", "127.0.0.1"),
        "PORT": env("DB_PORT", "3306"),
        "OPTIONS": {
            "charset": "utf8mb4",
            "init_command": "SET sql_mode='STRICT_TRANS_TABLES'",
        },
    }
}

CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": env("VALKEY_URL", "redis://127.0.0.1:6379/0"),
        "OPTIONS": {"CLIENT_CLASS": "django_redis.client.DefaultClient"},
    }
}

SESSION_ENGINE = "django.contrib.sessions.backends.cache"
SESSION_CACHE_ALIAS = "default"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
# Env-driven, like MEDIA_ROOT below. A deployment collects static into a directory nginx serves and
# the application never writes to again, and it must not be under the app directory: the deploy
# force-updates that from git, so anything collected there is liable to be thrown away.
#
# This was hardcoded while the Ansible role passed LUMINA_STATIC_ROOT and the vhost served
# /var/lib/lumina/static. Nothing complained. collectstatic wrote 239 files into the checkout,
# nginx served an empty directory, and every page came back 200 with no CSS or JavaScript at all.
STATIC_ROOT = env("LUMINA_STATIC_ROOT", str(BASE_DIR / "staticfiles"))
STATICFILES_DIRS = [BASE_DIR / "static"] if (BASE_DIR / "static").exists() else []

MEDIA_URL = "/media/"
MEDIA_ROOT = env("MEDIA_ROOT", str(BASE_DIR / "media"))

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Authentication / OIDC ---------------------------------------------------
AUTHENTICATION_BACKENDS = [
    "lumina.accounts.auth.LuminaOIDCBackend",
    "django.contrib.auth.backends.ModelBackend",
]
LOGIN_URL = "/oidc/authenticate/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/"

OIDC_RP_CLIENT_ID = env("OIDC_RP_CLIENT_ID", "")
OIDC_RP_CLIENT_SECRET = env("OIDC_RP_CLIENT_SECRET", "")
OIDC_OP_AUTHORIZATION_ENDPOINT = env("OIDC_OP_AUTHORIZATION_ENDPOINT", "")
OIDC_OP_TOKEN_ENDPOINT = env("OIDC_OP_TOKEN_ENDPOINT", "")
OIDC_OP_USER_ENDPOINT = env("OIDC_OP_USER_ENDPOINT", "")
OIDC_OP_JWKS_ENDPOINT = env("OIDC_OP_JWKS_ENDPOINT", "")
OIDC_RP_SIGN_ALGO = "RS256"
# Keycloak's own username for the account, rather than mozilla-django-oidc's default base64 SHA-224
# of the email address. See ``lumina.accounts.auth.username_from_claims``.
OIDC_USERNAME_ALGO = "lumina.accounts.auth.username_from_claims"
# Deliberately does NOT ask for a "groups" scope, though group membership is exactly what this
# application needs from Keycloak.
#
# Keycloak validates every requested scope against the client's assigned client scopes and rejects
# the whole authorization request if one is unknown: it redirects straight back with
# "error=invalid_scope&error_description=Invalid+scopes:+openid+email+profile+groups" and nobody can
# sign in at all. There is no built-in client scope called "groups" (the built-ins are acr, basic,
# email, profile, roles, web-origins and a few optional ones), so asking for it makes a realm that
# has not had one created by hand fail closed.
#
# It is also unnecessary. Verified against Keycloak 26: with the Group Membership mapper on the
# client's own dedicated scope, the "groups" claim arrives in both the access and ID tokens for a
# request asking only for "openid" - a mapper on the dedicated scope is unconditional. So the default
# is the request that works everywhere, and a realm that does have a "groups" client scope can ask
# for it by setting OIDC_RP_SCOPES.
OIDC_RP_SCOPES = env("OIDC_RP_SCOPES", "openid email profile")
# Keycloak group → Django group map. Keys are Keycloak group names or full group paths.
#
# Shipped populated on purpose. A deployment should not have to configure this to let its
# administrators in: an empty or wrong map means everyone signs in successfully with no permissions
# at all, and nothing anywhere reports an error, so the first person to hit it has to read this file
# to find out why. The AlmaLinux realm's group is plainly ``admins``, so that is what is here.
#
# ``LUMINA_OIDC_GROUP_MAP`` in the environment (Ansible: ``lumina_oidc_group_map``) **replaces** this
# map rather than adding to it, e.g.
#
#     LUMINA_OIDC_GROUP_MAP=lumina-admins=admin,lumina-reviewers=reviewer
#
# Replace and not merge, because the entries below are a grant and a deployment has to be able to
# take one away. A realm with its own unrelated ``admins`` group would otherwise hand every member
# of it is_staff/is_superuser here with no way to say no short of editing this file.
LUMINA_OIDC_GROUP_MAP = env_map(
    "LUMINA_OIDC_GROUP_MAP",
    {
        # The AlmaLinux realm's own names.
        "admins": "admin",
        # The prefixed spellings, kept so a realm using either convention works untouched. Both map
        # to the same Django group, and a user in both gets it once.
        "lumina-admins": "admin",
        "lumina-reviewers": "reviewer",
        # Certification SIG. Grants the authority to certify on AlmaLinux's behalf
        # and nothing else - unlike "admin", which this layer escalates to
        # is_staff/is_superuser further down.
        "certification-sig": "certifier",
    },
)
# Whether membership of a nested group also counts as membership of the groups it is nested inside.
#
# This is what makes an LDAP-style nested group work. In FreeIPA, putting the ``admins`` group into
# the ``lumina-admins`` group *is* how you say "the admins are Lumina admins", and Keycloak, when
# its LDAP group mapper has "Preserve Group Inheritance" on, imports that as the group path
# ``/lumina-admins/admins`` - so the groups claim names the child and never mentions the parent.
# Keycloak's own model does not propagate membership upward (a subgroup's members inherit the
# parent's *roles*, not its membership), so without this a map keyed on ``lumina-admins`` matches
# nothing at all and the sign-in grants nothing.
#
# On by default because it is the meaning nesting has in the directory that is the source of truth
# here. Turn it off for a realm that uses Keycloak-native subgroups to *narrow* a parent group
# rather than to feed it, where treating a child as the parent would over-grant. See
# ``lumina.accounts.auth.claimed_group_keys``.
LUMINA_OIDC_GROUP_NESTED_PARENTS = env_bool("LUMINA_OIDC_GROUP_NESTED_PARENTS", True)

# --- DRF ---------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
        "lumina.accounts.auth.ApiTokenAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticatedOrReadOnly",
    ],
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 25,
    # Ingest and device-auth endpoints are unauthenticated or cheap to hit
    # but expensive to serve, so they are throttled per scope. Backed by the
    # Valkey cache configured above.
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.ScopedRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "results-ingest": env("LUMINA_THROTTLE_INGEST", "30/hour"),
        "device-code": env("LUMINA_THROTTLE_DEVICE_CODE", "10/hour"),
        "device-token": env("LUMINA_THROTTLE_DEVICE_TOKEN", "120/hour"),
        # Generous: a fleet submits many machines through one account in a run.
        # Abuse is caught by moderation and a revocable token, not a tight cap.
        "survey-ingest": env("LUMINA_THROTTLE_SURVEY", "1000/hour"),
    },
}

# --- Email -------------------------------------------------------------------
EMAIL_BACKEND = env("EMAIL_BACKEND", "django.core.mail.backends.smtp.EmailBackend")
EMAIL_HOST = env("EMAIL_HOST", "localhost")
EMAIL_PORT = int(env("EMAIL_PORT", "25"))
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", "lumina@almalinux.org")
LUMINA_REVIEW_NOTIFY_EMAILS = env_list("LUMINA_REVIEW_NOTIFY_EMAILS", [])

# --- Notifications (webhooks + email, delivered out of band by deliver_notifications) --------
# Events that need someone to act are queued in-transaction and delivered by the
# ``deliver_notifications`` command (a systemd timer, every minute) - never in the request path.
LUMINA_NOTIFICATIONS_ENABLED = env_bool("LUMINA_NOTIFICATIONS_ENABLED", True)
# Per-event kill switch: event keys listed here are never enqueued. Keys: lumina/notifications/events.py.
LUMINA_NOTIFY_DISABLED_EVENTS = env_list("LUMINA_NOTIFY_DISABLED_EVENTS", [])
# Absolute base for links in emails/webhooks. No request is available in the drainer and there is no
# Sites framework, so it cannot be derived - defaults to the first allowed host over https.
LUMINA_SITE_BASE_URL = env(
    "LUMINA_SITE_BASE_URL",
    f"https://{ALLOWED_HOSTS[0]}" if ALLOWED_HOSTS else "http://localhost:8000",
)
LUMINA_WEBHOOK_TIMEOUT_SECONDS = int(env("LUMINA_WEBHOOK_TIMEOUT_SECONDS", "10"))
LUMINA_NOTIFY_MAX_ATTEMPTS = int(env("LUMINA_NOTIFY_MAX_ATTEMPTS", "5"))

# --- Unfold (Django admin theme) --------------------------------------------
# AlmaLinux brand palette: navy #082336, blue #004bbc. Unfold accepts a
# Tailwind-style 50..950 scale per color slot; the values below approximate
# the AlmaLinux blue across that range.
UNFOLD = {
    "SITE_TITLE": "Lumina Admin",
    "SITE_HEADER": "Lumina",
    "SITE_SUBHEADER": "AlmaLinux Hardware Certification",
    "SHOW_HISTORY": True,
    "SHOW_VIEW_ON_SITE": True,
    "COLORS": {
        "primary": {
            "50":  "238 245 255",
            "100": "215 230 255",
            "200": "175 205 255",
            "300": "126 173 245",
            "400": "76 138 222",
            "500": "0 75 188",   # AlmaLinux blue
            "600": "0 60 158",
            "700": "0 47 124",
            "800": "8 35 54",    # AlmaLinux navy
            "900": "5 22 36",
            "950": "3 14 24",
        },
    },
}

# --- Lumina-specific ---------------------------------------------------------
LUMINA_DEFAULT_COLLAPSED_LIMIT = 10
LUMINA_API_TOKEN_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 days max
# 12 hours: long enough that a full validate-plus-benchmark pass cannot
# outlive its own token mid-run.
LUMINA_CLI_TOKEN_TTL_SECONDS = 60 * 60 * 12

# --- Hardware survey ---------------------------------------------------------
LUMINA_SURVEY_ENABLED = env_bool("LUMINA_SURVEY_ENABLED", True)
# Secret pepper folded into every identity hash so a DB leak cannot be
# dictionary-attacked against guessed serials. Defaults to SECRET_KEY for dev;
# set explicitly in production.
LUMINA_SURVEY_IDENTITY_PEPPER = env("LUMINA_SURVEY_IDENTITY_PEPPER", SECRET_KEY)
# Longest survey/automation token a granted account may mint (default ~1 year),
# lifting the 30-day self-serve cap for approved accounts only.
LUMINA_SURVEY_MAX_TOKEN_TTL_SECONDS = int(
    env("LUMINA_SURVEY_MAX_TOKEN_TTL_SECONDS", str(60 * 60 * 24 * 366))
)

# --- Result ingestion ---------------------------------------------------------
# Hard cap on an uploaded bundle. nginx must allow at least this much too
# (client_max_body_size in the ansible role's vhost template).
LUMINA_BUNDLE_MAX_BYTES = int(env("LUMINA_BUNDLE_MAX_BYTES", str(256 * 1024 * 1024)))
# Zip-bomb guard: total uncompressed size allowed out of one bundle.
LUMINA_BUNDLE_MAX_EXTRACTED_BYTES = int(
    env("LUMINA_BUNDLE_MAX_EXTRACTED_BYTES", str(1024 * 1024 * 1024))
)
# Device-authorization flow (alma-cert register).
LUMINA_DEVICE_CODE_TTL_SECONDS = 15 * 60
LUMINA_DEVICE_POLL_INTERVAL_SECONDS = 5
LUMINA_DEVICE_MAX_PENDING_PER_IP = 3
# Rejected runs' bundles are pruned after this many days by prune_run_bundles.
LUMINA_REJECTED_BUNDLE_RETENTION_DAYS = 90
# How ``submit:attachment`` hands a file to the client once it has authorized the
# request.
#
# Empty means Django streams it itself, which is what the dev server and the test suite
# need. Set to the nginx prefix that fronts MEDIA_ROOT (``/media/``) and the view instead
# returns an empty response carrying ``X-Accel-Redirect``, letting nginx do the sending.
# The matching location in the vhost is marked ``internal``, so that path is reachable
# *only* through this handoff and never by a client asking for it directly.
#
# Submission evidence is the reason this exists: it is arbitrary files a submitter chose,
# it is reviewer material rather than catalog content, and it used to sit under the
# public ``/media/`` alias with no authorization of any kind.
LUMINA_INTERNAL_MEDIA_LOCATION = env("LUMINA_INTERNAL_MEDIA_LOCATION", "")

# --- Sentry (error monitoring; inert unless SENTRY_DSN is set) -------------------------------
# Captures unhandled exceptions - the 500s - with their traceback and request context, in dev and
# prod alike, tagged by environment. The Django integration auto-enables. ``send_default_pii`` stays
# off deliberately: we want the stack trace, not submitter emails or IP addresses, in a third-party
# service. A blank DSN (the default, and what tests and CI run with) leaves Sentry entirely disabled.
SENTRY_DSN = env("SENTRY_DSN", "")
if SENTRY_DSN:
    import sentry_sdk

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        environment=env("SENTRY_ENVIRONMENT", "production"),
        release=env("SENTRY_RELEASE", "") or None,
        traces_sample_rate=float(env("SENTRY_TRACES_SAMPLE_RATE", "0.0")),
        send_default_pii=False,
    )
