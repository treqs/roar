"""Unit tests for publish-intent resolution (visibility + attribution)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from roar.cli.publish_intent import resolve_publish_intent


def _resolve(public=None, anonymous=False, *, scope=None, logged_in=False, public_default=False):
    scope_obj = SimpleNamespace(mode=scope) if scope is not None else None
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


def test_unset_not_logged_in_defaults_anonymous_with_flag():
    out = _resolve(scope=None, logged_in=False)
    assert out.public and out.anonymous
    assert out.defaulted_anonymous  # drives the warning


def test_unset_public_by_default_config_goes_public():
    out = _resolve(scope=None, logged_in=False, public_default=True)
    assert out.public and not out.anonymous
    assert out.used_public_default
