"""The anonymous-register signup nudge: name the account-only unlock, once.

Covers the activation-funnel fix — after a first anonymous register, roar
names what a GLaaS account adds (private / team / cross-machine reproduce)
and points at `roar login`. It fires once per user and respects the hints
gate.
"""

from __future__ import annotations

from unittest.mock import patch

from roar.cli.commands.register import _maybe_show_signup_nudge


def test_nudge_names_login_and_account_value(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    _maybe_show_signup_nudge()
    err = capsys.readouterr().err
    assert "roar login" in err
    assert "private" in err
    # The local-vs-GLaaS difference (#2): reproduce elsewhere / share.
    assert "another machine" in err


def test_nudge_fires_only_once_per_user(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    _maybe_show_signup_nudge()
    assert "roar login" in capsys.readouterr().err

    # Second call is suppressed by the cache marker.
    _maybe_show_signup_nudge()
    assert capsys.readouterr().err == ""


def test_nudge_silent_when_hints_disabled(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    with patch("roar.cli._format.hints_should_print", return_value=False):
        _maybe_show_signup_nudge()
    assert capsys.readouterr().err == ""
    # Nothing shown -> marker not written, so a later (enabled) run still nudges.
    _maybe_show_signup_nudge()
    assert "roar login" in capsys.readouterr().err
