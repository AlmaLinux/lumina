"""Production settings. All secrets required from env."""
from .base import *  # noqa: F401,F403
from .base import env, env_bool, env_list

DEBUG = False
SECRET_KEY = env("DJANGO_SECRET_KEY", required=True)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", [])

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", True)
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
X_FRAME_OPTIONS = "DENY"

STATICFILES_STORAGE = "django.contrib.staticfiles.storage.ManifestStaticFilesStorage"
