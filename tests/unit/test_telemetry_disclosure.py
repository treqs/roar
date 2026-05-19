"""Tests for the one-time telemetry disclosure printed by the CLI dispatcher.

The disclosure is the *only* active push notification that tells the
user telemetry is enabled. It must:

  * print on the first invocation (any subcommand),
  * stay silent on subsequent invocations (sentinel-gated),
  * stay silent when telemetry is already disabled (no point disclosing
    to opted-out users),
  * stay silent when output gates wouldn't allow advisory hints (CI,
    redirected, quiet mode, hints disabled).

The gate must *not* write the sentinel when it skipped because the
output gate failed — otherwise a non-TTY first-run permanently hides
the disclosure from later interactive invocations.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path

import pytest

from roar.telemetry import paths
from roar.telemetry.hooks import maybe_print_telemetry_disclosure


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
        "XDG_CACHE_HOME": str(tmp_path / "xdg-cache"),
    }


@pytest.fixture
def hints_gate_open(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Force the shared hint gate open so we exercise the print path.

    Otherwise non-TTY CliRunner-style invocations short-circuit before
    the disclosure body runs.
    """
    from roar.cli import _format

    monkeypatch.setattr(_format, "hints_should_print", lambda: True)
    yield


def _capture_stdout(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    buf = io.StringIO()
    import click

    monkeypatch.setattr(click, "echo", lambda msg="", *a, **kw: buf.write(str(msg) + "\n"))
    return buf


def test_disclosure_prints_once_and_creates_sentinel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hints_gate_open: None
) -> None:
    env = _env(tmp_path)
    buf = _capture_stdout(monkeypatch)

    maybe_print_telemetry_disclosure(environ=env)

    output = buf.getvalue()
    assert "Telemetry: anonymous counters" in output
    assert "roar telemetry --disable" in output
    assert "DO_NOT_TRACK=1" in output

    resolved = paths.resolve_paths(env)
    assert resolved.disclosure_sentinel_file.exists()


def test_disclosure_silent_when_sentinel_already_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hints_gate_open: None
) -> None:
    env = _env(tmp_path)

    resolved = paths.resolve_paths(env)
    resolved.disclosure_sentinel_file.parent.mkdir(parents=True, exist_ok=True)
    resolved.disclosure_sentinel_file.touch()

    buf = _capture_stdout(monkeypatch)
    maybe_print_telemetry_disclosure(environ=env)

    assert buf.getvalue() == ""


def test_disclosure_silent_when_telemetry_already_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hints_gate_open: None
) -> None:
    """``DO_NOT_TRACK=1`` users have already opted out — telling them
    "telemetry is on, here's how to turn it off" is misleading and noisy.
    """
    env = {**_env(tmp_path), "DO_NOT_TRACK": "1"}
    buf = _capture_stdout(monkeypatch)

    maybe_print_telemetry_disclosure(environ=env)

    assert buf.getvalue() == ""
    resolved = paths.resolve_paths(env)
    # Sentinel must NOT be written: if the user later un-sets DO_NOT_TRACK,
    # they should still get a fresh disclosure.
    assert not resolved.disclosure_sentinel_file.exists()


def test_disclosure_silent_when_hints_gate_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-TTY / quiet / hints-disabled contexts get no disclosure and no
    sentinel write — so the next interactive invocation can show it.
    """
    env = _env(tmp_path)

    from roar.cli import _format

    monkeypatch.setattr(_format, "hints_should_print", lambda: False)
    buf = _capture_stdout(monkeypatch)

    maybe_print_telemetry_disclosure(environ=env)

    assert buf.getvalue() == ""
    resolved = paths.resolve_paths(env)
    assert not resolved.disclosure_sentinel_file.exists()


def test_disclosure_swallows_unexpected_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Telemetry must never break CLI use — a broken path resolver or
    full disk shouldn't surface a traceback to the user."""

    def explode(*_a, **_kw):
        raise OSError("disk full")

    # Patch the binding the disclosure function actually uses.
    monkeypatch.setattr("roar.telemetry.hooks.resolve_paths", explode)
    # No assertion — just confirm it returns without raising.
    maybe_print_telemetry_disclosure()
