"""Startup checks for configuration that would otherwise fail somewhere nobody is looking.

Email is the whole of it so far, and the reason is where it fails. Every message this system
sends goes through ``deliver_notifications``, a systemd timer with a retry ladder, so a
misconfigured relay does not break a page or raise into anybody's request: the drainer records
the error, backs off five times, and marks the delivery failed. The deploy looks clean and the
notifications quietly stop.

A system check runs on ``manage.py check``, and on ``migrate`` and ``collectstatic`` - which the
ansible role runs on every deploy - so this turns that silence into a failed deploy.
"""
from __future__ import annotations

from django.conf import settings
from django.core.checks import Error, Warning, register

# Ports where a hosted submission service expects the connection to be encrypted. 465 is
# implicit TLS; 587 is submission with STARTTLS required. 25 is left out on purpose: a local
# relay on the loopback is the one place plaintext is normal.
SECURE_SUBMISSION_PORTS = {465, 587}


@register()
def email_transport_is_usable(app_configs, **kwargs) -> list:
    """Refuse a relay configuration that cannot send, and flag one that probably should not.

    Only for a backend that actually opens a connection. The console and locmem backends ignore
    all of this, and the devstack uses the console one, so checking them would fail a stack that
    is working exactly as intended.
    """
    backend = getattr(settings, "EMAIL_BACKEND", "")
    if "smtp" not in backend:
        return []

    problems = []
    use_tls = getattr(settings, "EMAIL_USE_TLS", False)
    use_ssl = getattr(settings, "EMAIL_USE_SSL", False)
    user = getattr(settings, "EMAIL_HOST_USER", "")
    password = getattr(settings, "EMAIL_HOST_PASSWORD", "")
    port = getattr(settings, "EMAIL_PORT", None)

    if use_tls and use_ssl:
        problems.append(Error(
            "EMAIL_USE_TLS and EMAIL_USE_SSL are both set.",
            hint="STARTTLS on port 587 is EMAIL_USE_TLS; implicit TLS on 465 is EMAIL_USE_SSL. "
                 "Django refuses both together, and it refuses them when it connects - which "
                 "here is inside deliver_notifications, where the only trace is a failed "
                 "delivery. Pick the one that matches EMAIL_PORT.",
            id="lumina.E001",
        ))

    if (user or password) and not (use_tls or use_ssl):
        problems.append(Warning(
            "EMAIL_HOST_USER is set but the connection is not encrypted.",
            hint="These credentials would cross the network in the clear, and a hosted relay "
                 "will refuse the login anyway - SES requires TLS on both submission ports. "
                 "Set EMAIL_USE_TLS for port 587 or EMAIL_USE_SSL for 465.",
            id="lumina.W001",
        ))

    if port in SECURE_SUBMISSION_PORTS and not (use_tls or use_ssl):
        problems.append(Warning(
            f"EMAIL_PORT is {port} but neither EMAIL_USE_TLS nor EMAIL_USE_SSL is set.",
            hint="Both submission ports expect an encrypted connection: 587 requires STARTTLS "
                 "(EMAIL_USE_TLS) and 465 is implicit TLS (EMAIL_USE_SSL). As it stands the "
                 "relay will hang up.",
            id="lumina.W002",
        ))

    if (password and not user) or (user and not password):
        problems.append(Warning(
            "EMAIL_HOST_USER and EMAIL_HOST_PASSWORD: one is set and the other is not.",
            hint="Half a credential authenticates nothing. SES SMTP credentials are a pair, "
                 "generated together in the SES console - they are not AWS access keys.",
            id="lumina.W003",
        ))

    return problems
