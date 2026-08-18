"""The environment parsers in ``lumina.settings.base``.

``env_map`` is the one worth pinning. It backs ``LUMINA_OIDC_GROUP_MAP``, which decides who gets
administrative access to a deployment, and it is read at import time - so a malformed value has to
fail loudly at startup rather than resolve to a partial map, which would look like a working
deployment where some people quietly have no permissions.
"""
from __future__ import annotations

import pytest

from lumina.settings.base import env_map

DEFAULT = {"admins": "admin"}


def _with(monkeypatch, value):
    monkeypatch.setenv("A_MAP", value)
    return env_map("A_MAP", DEFAULT)


def test_unset_gives_the_default(monkeypatch):
    monkeypatch.delenv("A_MAP", raising=False)
    assert env_map("A_MAP", DEFAULT) == DEFAULT


def test_empty_gives_the_default(monkeypatch):
    """Deliberately not "configured to grant nothing": an env file that writes the variable with no
    value is an unconfigured deployment, and it should still let its administrators in."""
    assert _with(monkeypatch, "   ") == DEFAULT


def test_pairs_are_parsed(monkeypatch):
    assert _with(monkeypatch, "lumina-admins=admin,lumina-reviewers=reviewer") == {
        "lumina-admins": "admin",
        "lumina-reviewers": "reviewer",
    }


def test_whitespace_around_pairs_is_ignored(monkeypatch):
    assert _with(monkeypatch, " a = admin , b = reviewer ") == {"a": "admin", "b": "reviewer"}


def test_a_configured_map_replaces_rather_than_extends(monkeypatch):
    """The entries in the default are grants, and a deployment has to be able to take one away: a
    realm with its own unrelated ``admins`` group must not be handed superuser with no way to say
    no."""
    assert _with(monkeypatch, "lumina-admins=admin") == {"lumina-admins": "admin"}
    assert "admins" not in _with(monkeypatch, "lumina-admins=admin")


def test_group_paths_are_usable_as_keys(monkeypatch):
    """Keys may be full Keycloak group paths, which contain slashes but never ``=`` or ``,``."""
    assert _with(monkeypatch, "almalinux/lumina-admins=admin") == {
        "almalinux/lumina-admins": "admin"
    }


@pytest.mark.parametrize("value", ["admin", "a=admin,reviewer", "=admin", "a="])
def test_a_malformed_pair_raises(monkeypatch, value):
    with pytest.raises(RuntimeError, match="A_MAP"):
        _with(monkeypatch, value)
