"""Unit tests for the hint/banner primitives in ``roar.cli._format``.

The end-to-end behavior is also exercised indirectly via
``test_cli_init.py`` etc.; these tests lock in the *stream* invariant
and the new gate semantics (no TTY check) at the layer where they live.
"""

from __future__ import annotations

import io
import sys
from unittest.mock import patch

import pytest

from roar.cli import _format


def test_hints_should_print_default_true(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no config overrides, hints fire — regardless of TTY status."""

    def _config_get(key: str, **_kwargs: object) -> object:
        # Mirror the real defaults from access.py
        defaults = {"hints.enabled": True, "output.verbosity": "normal"}
        return defaults.get(key)

    monkeypatch.setattr("roar.integrations.config.config_get", _config_get)
    # The TTY check is gone, so even a piped stdout/stderr should be True.
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    assert _format.hints_should_print() is True


def test_hints_should_print_respects_config_disable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "roar.integrations.config.config_get",
        lambda key, **_kwargs: False if key == "hints.enabled" else None,
    )
    assert _format.hints_should_print() is False


def test_hints_should_print_respects_quiet_verbosity(monkeypatch: pytest.MonkeyPatch) -> None:
    def _config_get(key: str, **_kwargs: object) -> object:
        return {"hints.enabled": True, "output.verbosity": "quiet"}.get(key)

    monkeypatch.setattr("roar.integrations.config.config_get", _config_get)
    assert _format.hints_should_print() is False


def test_hint_printer_writes_to_stderr_not_stdout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The whole point of this surface: hints land on stderr."""
    _caps, hint = _format.make_hint_printer()
    hint("next: roar dag")
    captured = capsys.readouterr()
    assert "hint: next: roar dag" in captured.err
    assert "hint:" not in captured.out


def test_print_brand_header_writes_to_stderr_not_stdout(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Brand header follows the same convention as hints — on stderr,
    so stdout stays clean for pipelines."""

    def _config_get(key: str, **_kwargs: object) -> object:
        return {"hints.enabled": True, "output.verbosity": "normal"}.get(key)

    monkeypatch.setattr("roar.integrations.config.config_get", _config_get)
    with patch("roar.cli._format.detect") as detect_mock:
        detect_mock.return_value.can_emoji = False
        detect_mock.return_value.can_color = False
        _format.print_brand_header("dag")
    captured = capsys.readouterr()
    assert "roar: roar dag" in captured.err
    assert "roar: roar dag" not in captured.out


def test_brand_header_suppressed_when_hints_disabled(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "roar.integrations.config.config_get",
        lambda key, **_kwargs: False if key == "hints.enabled" else None,
    )
    _format.print_brand_header("dag")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
