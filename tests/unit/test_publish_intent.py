"""Unit tests for publish-intent resolution (visibility + attribution)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from roar.cli.publish_intent import resolve_publish_intent


def _resolve(
    public=None,
    anonymous=False,
    *,
    scope=None,
    visibility=None,
    logged_in=False,
    public_default=False,
):
    scope_obj = SimpleNamespace(mode=scope, visibility=visibility) if scope is not None else None
    with (
        patch("roar.scope_config.load_repo_scope", return_value=scope_obj),
        patch("roar.cli.publish_intent._is_logged_in", return_value=logged_in),
        patch("roar.integrations.config.config_get", return_value=public_default),
    ):
        return resolve_publish_intent(public, anonymous)


def test_explicit_flags_win():
    assert _resolve(anonymous=True) == _resolve(anonymous=True)  # stable
    a = _resolve(anonymous=True)
    assert a.public and a.anonymous
    pub = _resolve(public=True, logged_in=True)
    assert pub.public and not pub.anonymous
    priv = _resolve(public=False, logged_in=False)
    assert not priv.public and not priv.anonymous


def test_explicit_scope_is_honored_even_when_logged_in():
    # Deliberately-anonymous repo stays anonymous despite being signed in.
    anon = _resolve(scope="anonymous", logged_in=True)
    assert anon.public and anon.anonymous
    assert not anon.defaulted_anonymous  # chosen, not a fallback
    priv = _resolve(scope="private", logged_in=False)
    assert not priv.public and not priv.anonymous


def test_unset_logged_in_defaults_private():
    out = _resolve(scope=None, logged_in=True)
    assert not out.public and not out.anonymous
    assert not out.defaulted_anonymous


def test_delegated_task_defaults_private_without_auth_file(monkeypatch):
    monkeypatch.setenv("ROAR_DELEGATED_AUTH", "1")
    monkeypatch.setenv("ROAR_DELEGATED_VISIBILITY", "private")
    with (
        patch("roar.scope_config.load_repo_scope", return_value=None),
        patch("roar.auth_store.load_auth_state", return_value=None),
        patch("roar.integrations.config.config_get", return_value=False),
    ):
        out = resolve_publish_intent(None, False)

    assert not out.public and not out.anonymous


def test_delegated_task_uses_frozen_visibility_over_repo_and_flags(monkeypatch):
    monkeypatch.setenv("ROAR_DELEGATED_AUTH", "1")
    monkeypatch.setenv("ROAR_DELEGATED_VISIBILITY", "public")
    with patch(
        "roar.scope_config.load_repo_scope",
        return_value=SimpleNamespace(mode="anonymous", visibility=None),
    ) as load_repo_scope:
        out = resolve_publish_intent(public=False, anonymous=True)

    assert out.public and not out.anonymous
    load_repo_scope.assert_not_called()


def test_unset_not_logged_in_defaults_anonymous_with_flag():
    out = _resolve(scope=None, logged_in=False)
    assert out.public and out.anonymous
    assert out.defaulted_anonymous  # drives the warning


def test_project_scope_public_project_defaults_public():
    # A project scope bound to a PUBLIC project defaults the DAG to public.
    out = _resolve(scope="project", visibility="public", logged_in=True)
    assert out.public and not out.anonymous


def test_project_scope_private_project_defaults_private():
    out = _resolve(scope="project", visibility="private", logged_in=True)
    assert not out.public and not out.anonymous


def test_project_scope_unknown_visibility_defaults_private():
    # Backward compat: a project binding without visibility stays private.
    out = _resolve(scope="project", visibility=None, logged_in=True)
    assert not out.public and not out.anonymous


def test_explicit_private_overrides_public_project_scope():
    out = _resolve(public=False, scope="project", visibility="public", logged_in=True)
    assert not out.public and not out.anonymous


def test_unset_public_by_default_config_goes_public():
    out = _resolve(scope=None, logged_in=False, public_default=True)
    assert out.public and not out.anonymous
    assert out.used_public_default
