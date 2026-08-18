"""The action bar's Run button on the User and Group changelists.

Every model in the project registers with Unfold's ``ModelAdmin``, but ``auth.User`` and
``auth.Group`` arrive pre-registered by ``django.contrib.auth`` against plain
``admin.ModelAdmin``, and nothing here had ever unregistered them. Unfold's action bar draws its
submit control as ``<button x-show="action">`` over an Alpine scope of ``{action: ''}``, and the
only thing that ever writes to ``action`` is the ``x-model`` attribute Unfold's own ``ActionForm``
puts on the select. Django's ``ActionForm`` has no such attribute, so on these two changelists
``action`` stayed empty, the button stayed hidden, and selecting rows and picking
"Delete selected users" gave an action bar with nothing to press.
"""
from __future__ import annotations

import pytest
from django.contrib import admin
from django.contrib.auth.models import Group, User
from django.test import Client
from django.urls import reverse
from unfold.admin import ModelAdmin
from unfold.forms import UserChangeForm as UnfoldUserChangeForm

pytestmark = pytest.mark.django_db


@pytest.fixture
def staff_client(db):
    User.objects.create_superuser("admin-tester", "admin-tester@example.com", "pw")
    client = Client()
    client.force_login(User.objects.get(username="admin-tester"))
    return client


@pytest.mark.parametrize("model", [User, Group])
def test_auth_changelists_are_registered_with_unfold(model):
    """A plain ``admin.ModelAdmin`` here renders Django's action form into Unfold's action bar,
    and the bar cannot submit it."""
    assert isinstance(admin.site._registry[model], ModelAdmin)


@pytest.mark.parametrize(
    "url_name", ["admin:auth_user_changelist", "admin:auth_group_changelist"]
)
def test_the_action_select_binds_to_alpine_so_the_run_button_can_appear(staff_client, url_name):
    """``x-model="action"`` is the whole mechanism: without it the ``x-show="action"`` button in
    Unfold's action bar is unreachable no matter how many rows are selected."""
    html = staff_client.get(reverse(url_name)).content.decode()
    assert 'x-model="action"' in html
    assert 'x-show="action"' in html


@pytest.mark.parametrize(
    "url_name", ["admin:auth_user_changelist", "admin:auth_group_changelist"]
)
def test_the_action_select_carries_no_stray_label(staff_client, url_name):
    """Django's ``ActionForm`` labels the field "Action:"; Unfold's leaves it empty and lets the
    bar speak for itself. The label showing up is the visible tell that the wrong form is in use."""
    html = staff_client.get(reverse(url_name)).content.decode()
    assert "Action:" not in html


def test_the_user_change_form_is_unfolds(staff_client):
    """Same origin, second symptom: Django's form pairs its password hash and permission fields
    with unstyled widgets that do not match anything else in this admin."""
    user = User.objects.create_user("subject", "subject@example.com", "pw")
    response = staff_client.get(reverse("admin:auth_user_change", args=[user.pk]))
    assert response.status_code == 200
    # ``modelform_factory`` hands back a subclass, so the base is what identifies the form.
    form = response.context["adminform"].form
    assert UnfoldUserChangeForm in type(form).__mro__
