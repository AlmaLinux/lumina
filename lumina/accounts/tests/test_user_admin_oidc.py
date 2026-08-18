"""The User admin flags OIDC-authenticated accounts and warns that their synced groups are owned by
Keycloak.

Reported: an admin can add or remove a user's groups here, but for the OIDC-managed groups the
change is silently reverted on the user's next login (``lumina.accounts.auth._sync_groups``), so the
admin needs to see which accounts are externally authenticated and that hand edits to those groups
will not stick. Not a hard lock, because the same widget legitimately assigns *unmanaged* groups,
which the sync leaves alone.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

pytestmark = pytest.mark.django_db

WARNING = "overwritten at the next sign-in"


@pytest.fixture
def staff_client(db):
    User.objects.create_superuser("admin-tester", "admin-tester@example.com", "pw")
    client = Client()
    client.force_login(User.objects.get(username="admin-tester"))
    return client


@pytest.fixture
def oidc_user(db):
    """Externally authenticated: provisioned by the OIDC login, never given a local password."""
    user = User.objects.create(username="oidc-person", email="oidc@example.com")
    user.set_unusable_password()
    user.save()
    return user


def test_the_change_form_warns_that_an_oidc_accounts_groups_are_overwritten(staff_client, oidc_user):
    body = staff_client.get(reverse("admin:auth_user_change", args=[oidc_user.pk])).content.decode()

    assert "OIDC" in body
    assert WARNING in body


def test_a_local_account_gets_no_such_warning(staff_client):
    """A local-password account is not driven by the identity provider, so the warning would be
    noise. This is the guard against warning on every account once OIDC is configured."""
    local = User.objects.create_user("local-person", "local@example.com", "pw")

    body = staff_client.get(reverse("admin:auth_user_change", args=[local.pk])).content.decode()

    assert WARNING not in body


def test_the_warning_is_silent_when_oidc_is_not_configured(staff_client, oidc_user, settings):
    """A devstack host uses the password login and has no OIDC endpoints; the group sync never runs
    there, so an account without a usable password must not be labelled as OIDC-managed."""
    settings.OIDC_OP_AUTHORIZATION_ENDPOINT = ""

    body = staff_client.get(reverse("admin:auth_user_change", args=[oidc_user.pk])).content.decode()

    assert WARNING not in body


def test_the_changelist_labels_each_accounts_sign_in_source(staff_client, oidc_user):
    body = staff_client.get(reverse("admin:auth_user_changelist")).content.decode()

    assert "OIDC (external)" in body   # the oidc_user
    assert "Local password" in body    # the superuser from staff_client
