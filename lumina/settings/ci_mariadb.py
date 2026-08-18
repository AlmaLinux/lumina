"""CI settings: the test profile pointed at a real MariaDB service container.

Catches engine-specific behavior the SQLite unit run cannot: JSONField
querying, CheckConstraint enforcement, DecimalField ordering, and charset
handling.
"""
from .base import env  # noqa: E402
from .test import *  # noqa: F401,F403

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.mysql",
        "NAME": env("DB_NAME", "lumina_test"),
        "USER": env("DB_USER", "lumina"),
        "PASSWORD": env("DB_PASSWORD", "lumina"),
        "HOST": env("DB_HOST", "127.0.0.1"),
        "PORT": env("DB_PORT", "3306"),
        "OPTIONS": {"charset": "utf8mb4"},
        "TEST": {"CHARSET": "utf8mb4"},
    }
}
