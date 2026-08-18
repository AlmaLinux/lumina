"""The relay settings reach Django, and a relay that cannot send says so at startup.

Reported from a deployment: EMAIL_HOST_USER, EMAIL_HOST_PASSWORD, and EMAIL_USE_TLS were written
into the environment file and ``settings/base.py`` read only host and port, so the credentials
were loaded by nothing and SES refused every connection.

What made that worth a check as well as a fix is *where* it failed. Nothing in this system sends
mail in a request: it all goes through ``deliver_notifications``, a systemd timer with a retry
ladder, so a relay that will not accept the connection produces no traceback and no broken page.
The drainer records the error, backs off five times, marks the delivery failed, and the
notifications stop arriving with nothing to see. So the settings are read, and a configuration
that cannot work fails the deploy instead - ``migrate`` and ``collectstatic`` both run the system
checks, and the role runs both.
"""
from __future__ import annotations

import importlib
import os
from unittest import mock

import pytest
from django.test import override_settings

SES = "email-smtp.us-east-1.amazonaws.com"
SMTP = "django.core.mail.backends.smtp.EmailBackend"


def reloaded(**environ):
    """``settings/base`` with these environment variables, read fresh.

    The module reads ``os.environ`` at import, which is the thing under test: a setting that is
    not read there is a setting the environment cannot set.
    """
    from lumina.settings import base

    with mock.patch.dict(os.environ, environ, clear=False):
        return importlib.reload(base)


@pytest.fixture(autouse=True)
def _restore_base():
    """Put the module back, so a reload here cannot leak into another test's settings."""
    yield
    from lumina.settings import base

    importlib.reload(base)


# --- the settings are read at all ------------------------------------------------


def test_the_credentials_reach_django():
    """The report. These three were in the environment file and read by nothing."""
    base = reloaded(
        EMAIL_HOST_USER="AKIAIOSFODNN7EXAMPLE",
        EMAIL_HOST_PASSWORD="ses-smtp-password",
        EMAIL_USE_TLS="1",
    )

    assert base.EMAIL_HOST_USER == "AKIAIOSFODNN7EXAMPLE"
    assert base.EMAIL_HOST_PASSWORD == "ses-smtp-password"
    assert base.EMAIL_USE_TLS is True


def test_implicit_tls_is_separately_settable():
    """Port 465 rather than 587, which is a different setting and not a synonym."""
    base = reloaded(EMAIL_USE_SSL="1")

    assert base.EMAIL_USE_SSL is True
    assert base.EMAIL_USE_TLS is False


def test_a_local_relay_still_needs_nothing():
    """The default has to stay a plain loopback MTA: the devstack and any host with its own
    postfix are configured by saying nothing at all."""
    base = reloaded()

    assert (base.EMAIL_HOST, base.EMAIL_PORT) == ("localhost", 25)
    assert base.EMAIL_HOST_USER == ""
    assert base.EMAIL_USE_TLS is False and base.EMAIL_USE_SSL is False


def test_the_relay_is_not_waited_on_forever():
    """Django's own default is None. The drainer is a timer firing every minute, so a relay that
    accepts a connection and never answers would stack runs up behind it."""
    base = reloaded()

    assert base.EMAIL_TIMEOUT == 30


def test_the_timeout_is_settable():
    assert reloaded(EMAIL_TIMEOUT="5").EMAIL_TIMEOUT == 5


# --- and a configuration that cannot send is refused -----------------------------


def registered_check():
    """The check, from Django's registry rather than by importing the module.

    Deliberately not ``from lumina.core.checks import ...``: ``@register()`` fires on import, so
    a test that imports the module registers the check itself and can no longer tell whether
    anything else would have. ``CoreConfig.ready`` is the real chain, and this is the only way
    to hold it - drop that import and this stops finding the check.
    """
    from django.core.checks.registry import registry

    for check in registry.get_checks():
        if getattr(check, "__name__", "") == "email_transport_is_usable":
            return check
    raise AssertionError("the email check is not registered; see CoreConfig.ready")


def problems(**settings) -> dict[str, str]:
    """The check's findings by id, under these settings."""
    check = registered_check()
    with override_settings(EMAIL_BACKEND=SMTP, **settings):
        return {p.id: str(p.msg) for p in check(None)}


def test_both_tls_flags_together_is_an_error():
    """Django refuses the pair when it connects, and here that is inside the drainer - where the
    only trace is a delivery that failed five times. This is the same refusal, at a moment a
    deploy can see."""
    found = problems(EMAIL_USE_TLS=True, EMAIL_USE_SSL=True, EMAIL_PORT=587)

    assert "lumina.E001" in found


def test_a_working_ses_configuration_is_quiet():
    """The shape the docs tell somebody to write. It has to pass without a warning, or the
    warnings mean nothing."""
    found = problems(
        EMAIL_HOST=SES, EMAIL_PORT=587, EMAIL_USE_TLS=True, EMAIL_USE_SSL=False,
        EMAIL_HOST_USER="AKIAIOSFODNN7EXAMPLE", EMAIL_HOST_PASSWORD="secret",
    )

    assert found == {}


def test_credentials_without_encryption_are_flagged():
    """They would cross the network in the clear, and SES would refuse the login regardless."""
    found = problems(
        EMAIL_HOST=SES, EMAIL_PORT=25, EMAIL_HOST_USER="user", EMAIL_HOST_PASSWORD="secret",
        EMAIL_USE_TLS=False, EMAIL_USE_SSL=False,
    )

    assert "lumina.W001" in found


@pytest.mark.parametrize("port", [587, 465])
def test_a_submission_port_without_encryption_is_flagged(port):
    """Both submission ports expect an encrypted connection; as configured the relay hangs up."""
    found = problems(EMAIL_PORT=port, EMAIL_USE_TLS=False, EMAIL_USE_SSL=False)

    assert "lumina.W002" in found


def test_port_25_is_not_flagged():
    """A local relay on the loopback is the one place plaintext is ordinary, and it is the
    default - warning about it would mean every devstack and every host with its own postfix
    started reporting a problem it does not have."""
    found = problems(EMAIL_PORT=25, EMAIL_USE_TLS=False, EMAIL_USE_SSL=False)

    assert found == {}


def test_half_a_credential_is_flagged():
    """A password with no username authenticates nothing, and SES credentials are issued as a
    pair - so one without the other is a copy-paste that went wrong."""
    found = problems(
        EMAIL_PORT=587, EMAIL_USE_TLS=True, EMAIL_HOST_PASSWORD="secret", EMAIL_HOST_USER="",
    )

    assert "lumina.W003" in found


def test_a_backend_that_sends_nowhere_is_left_alone():
    """The console backend ignores every one of these settings, and it is what the devstack
    runs. Checking it would fail a stack that is working exactly as intended."""
    check = registered_check()
    with override_settings(
        EMAIL_BACKEND="django.core.mail.backends.console.EmailBackend",
        EMAIL_USE_TLS=True, EMAIL_USE_SSL=True, EMAIL_PORT=587,
    ):
        assert check(None) == []


# --- what the backend Django builds actually carries -----------------------------


def test_the_smtp_backend_is_built_with_the_ses_credentials():
    """The end of the wiring, and the only version of this that proves SES will work: the
    connection object Django hands the drainer, with the values on it.

    Reading the settings module says they were parsed; this says they arrive.
    """
    from django.core.mail import get_connection

    with override_settings(
        EMAIL_BACKEND=SMTP, EMAIL_HOST=SES, EMAIL_PORT=587, EMAIL_USE_TLS=True,
        EMAIL_USE_SSL=False, EMAIL_HOST_USER="AKIAIOSFODNN7EXAMPLE",
        EMAIL_HOST_PASSWORD="ses-smtp-password", EMAIL_TIMEOUT=30,
    ):
        connection = get_connection()

    assert (connection.host, connection.port) == (SES, 587)
    assert connection.username == "AKIAIOSFODNN7EXAMPLE"
    assert connection.password == "ses-smtp-password"
    assert connection.use_tls is True
    assert connection.timeout == 30


def test_django_really_does_refuse_both_tls_flags():
    """The premise of ``lumina.E001``. If Django ever stopped refusing the pair, the check would
    be inventing a rule of its own and should be removed rather than left to confuse somebody."""
    from django.core.mail import get_connection

    with override_settings(EMAIL_BACKEND=SMTP, EMAIL_USE_TLS=True, EMAIL_USE_SSL=True):
        with pytest.raises(ValueError, match="mutually exclusive"):
            get_connection()


def test_manage_py_check_is_what_fails_the_deploy():
    """Through Django's registry and its own command, not by calling the function.

    This is the feature: the role runs ``migrate`` and ``collectstatic``, both of which run the
    system checks, so a relay that cannot send stops the deploy instead of stopping the mail. A
    check that is written and never registered reads exactly like one that works.
    """
    from django.core.management import call_command
    from django.core.management.base import SystemCheckError

    with override_settings(EMAIL_BACKEND=SMTP, EMAIL_USE_TLS=True, EMAIL_USE_SSL=True):
        with pytest.raises(SystemCheckError, match="lumina.E001"):
            call_command("check")


def test_a_warning_does_not_fail_the_deploy():
    """The four findings are not equally bad. Both TLS flags cannot work at all; credentials on
    port 25 might be a local relay somebody means. A warning that halted a deploy would get
    silenced, and then the error would be silenced with it."""
    from django.core.management import call_command

    with override_settings(
        EMAIL_BACKEND=SMTP, EMAIL_PORT=587, EMAIL_USE_TLS=False, EMAIL_USE_SSL=False,
    ):
        call_command("check")  # warns, returns
