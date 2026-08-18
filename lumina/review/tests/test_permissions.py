"""Tests for review-app permissions.

- ``is_reviewer(user)`` is True when the user is in the Django ``reviewer``
  or ``admin`` group (admins are implicit reviewers).
- ``is_reviewer`` is False for anonymous users and for plain authenticated
  users.
- The ``reviewer_required`` decorator returns a 403 for non-reviewers and
  passes through for reviewers.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser, Group
from django.http import HttpResponse
from django.test import RequestFactory

from lumina.review.permissions import is_reviewer, reviewer_required

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture
def plain_user():
    return User.objects.create_user(username="plain")


@pytest.fixture
def reviewer_user():
    u = User.objects.create_user(username="rev")
    u.groups.add(Group.objects.create(name="reviewer"))
    return u


@pytest.fixture
def admin_user():
    u = User.objects.create_user(username="adm")
    u.groups.add(Group.objects.create(name="admin"))
    return u


class IsReviewerTests:
    def test_anonymous_is_not_reviewer(self):
        assert is_reviewer(AnonymousUser()) is False

    def test_plain_user_is_not_reviewer(self, plain_user):
        assert is_reviewer(plain_user) is False

    def test_reviewer_group(self, reviewer_user):
        assert is_reviewer(reviewer_user) is True

    def test_admin_is_implicit_reviewer(self, admin_user):
        assert is_reviewer(admin_user) is True


class ReviewerRequiredDecoratorTests:
    def _call(self, user):
        factory = RequestFactory()
        request = factory.get("/review/")
        request.user = user

        @reviewer_required
        def view(r):
            return HttpResponse("ok")

        return view(request)

    def test_reviewer_passes(self, reviewer_user):
        resp = self._call(reviewer_user)
        assert resp.status_code == 200
        assert resp.content == b"ok"

    def test_plain_user_forbidden(self, plain_user):
        resp = self._call(plain_user)
        assert resp.status_code == 403

    def test_anonymous_forbidden(self):
        resp = self._call(AnonymousUser())
        assert resp.status_code == 403
