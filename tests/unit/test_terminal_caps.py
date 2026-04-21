"""Tests for presenters.terminal capability detection and styling."""

from __future__ import annotations

import pytest

from roar.presenters.terminal import detect, style


class _FakeStream:
    def __init__(self, tty: bool, encoding: str = "utf-8"):
        self.encoding = encoding
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


class TestDetect:
    def test_tty_utf8_enables_everything(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("LANG", "en_US.UTF-8")
        monkeypatch.delenv("NO_COLOR", raising=False)
        caps = detect(_FakeStream(tty=True, encoding="utf-8"))
        assert caps.is_tty
        assert caps.can_color
        assert caps.can_emoji

    def test_non_tty_disables_everything(self):
        caps = detect(_FakeStream(tty=False, encoding="utf-8"))
        assert not caps.is_tty
        assert not caps.can_color
        assert not caps.can_emoji
        assert caps.pipe_mode is True

    def test_no_color_env_disables_color_only(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("NO_COLOR", "1")
        monkeypatch.setenv("LANG", "en_US.UTF-8")
        caps = detect(_FakeStream(tty=True, encoding="utf-8"))
        assert caps.is_tty
        assert not caps.can_color
        assert caps.can_emoji  # emoji is still fine without color

    def test_ascii_encoding_disables_emoji(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("LANG", "C")
        monkeypatch.delenv("LC_ALL", raising=False)
        caps = detect(_FakeStream(tty=True, encoding="ascii"))
        assert caps.is_tty
        assert not caps.can_emoji

    def test_width_has_sensible_floor(self):
        caps = detect(_FakeStream(tty=True, encoding="utf-8"))
        assert caps.width >= 40


class TestStyle:
    def test_disabled_returns_plain_text(self):
        assert style("hello", "bold", "red", enabled=False) == "hello"

    def test_no_codes_returns_plain_text(self):
        assert style("hello") == "hello"

    def test_wraps_with_ansi(self):
        out = style("hi", "bold", enabled=True)
        assert "\x1b[1m" in out
        assert "\x1b[0m" in out
        assert "hi" in out

    def test_unknown_code_is_ignored(self):
        out = style("hi", "nonexistent", enabled=True)
        # Nothing to add → raw text with reset, but we skip wrapping when no prefix.
        assert out == "hi"
