"""CommandNotFoundError suggests an available near-miss command.

The common stumble: a `python`-only example copied onto a box that only has
`python3` (or vice versa — Windows/conda ship `python`, many Linux/macOS only
`python3`). The error should point at whichever interpreter actually exists.
"""

from __future__ import annotations

import shutil

import pytest

from roar.core.exceptions import CommandNotFoundError


def test_suggests_python3_when_only_python3_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda c: "/usr/bin/python3" if c == "python3" else None)
    msg = str(CommandNotFoundError("python"))
    assert "command not found: python" in msg
    assert "did you mean 'python3'?" in msg


def test_suggests_python_when_only_python_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda c: "/usr/bin/python" if c == "python" else None)
    assert "did you mean 'python'?" in str(CommandNotFoundError("python3"))


def test_suggests_pip3(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda c: "/x/pip3" if c == "pip3" else None)
    assert "did you mean 'pip3'?" in str(CommandNotFoundError("pip"))


def test_no_suggestion_when_alternative_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _c: None)
    msg = str(CommandNotFoundError("frobnicate"))
    assert "command not found: frobnicate" in msg
    assert "did you mean" not in msg


def test_no_suggestion_for_path_like_command(monkeypatch: pytest.MonkeyPatch) -> None:
    # A path was given, not a bare name — don't propose a sibling on PATH.
    monkeypatch.setattr(shutil, "which", lambda _c: "/anything")
    assert "did you mean" not in str(CommandNotFoundError("/usr/bin/python"))


def test_exit_code_is_127() -> None:
    assert CommandNotFoundError("python").exit_code == 127
