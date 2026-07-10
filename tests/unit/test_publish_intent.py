"""Unit tests for publish-intent resolution (visibility + attribution)."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from roar.cli.publish_intent import _format_elapsed, _in_flight_run_warnings, resolve_publish_intent
from roar.execution.runtime.active_runs import write_marker


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


def test_format_elapsed_under_a_minute():
    assert _format_elapsed(43) == "43s"


def test_format_elapsed_over_a_minute():
    assert _format_elapsed(134) == "2m14s"


def test_in_flight_run_warnings_empty_when_roar_dir_is_none():
    assert _in_flight_run_warnings(None) == []


def test_in_flight_run_warnings_empty_with_no_markers(tmp_path: Path):
    assert _in_flight_run_warnings(tmp_path / ".roar") == []


def test_in_flight_run_warnings_excludes_own_pid(tmp_path: Path):
    roar_dir = tmp_path / ".roar"
    write_marker(roar_dir, pid=os.getpid(), command=["python", "train.py"], job_type="run")
    assert _in_flight_run_warnings(roar_dir) == []


def test_in_flight_run_warnings_reports_other_live_pid(tmp_path: Path):
    roar_dir = tmp_path / ".roar"
    other_pid = os.getppid()  # guaranteed alive for the duration of the test
    write_marker(roar_dir, pid=other_pid, command=["python", "train.py"], job_type="build")

    warnings = _in_flight_run_warnings(roar_dir)

    assert len(warnings) == 1
    assert f"pid {other_pid}" in warnings[0]
    assert "train.py" in warnings[0]
    assert "roar build" in warnings[0]
    assert "still be in progress" in warnings[0]
