from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

import pytest

from roar.execution.runtime import abi_probe


def test_returns_none_for_empty_executable() -> None:
    assert abi_probe.probe_python_abi("") is None


def test_returns_none_for_non_python_basename(monkeypatch: pytest.MonkeyPatch) -> None:
    # Should bail before invoking subprocess for things that don't look like Python.
    called = MagicMock()
    monkeypatch.setattr(subprocess, "run", called)
    assert abi_probe.probe_python_abi("/bin/bash") is None
    assert called.call_count == 0


def test_parses_tag_from_successful_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        MagicMock(return_value=MagicMock(returncode=0, stdout="cp312\n", stderr="")),
    )
    assert abi_probe.probe_python_abi("/usr/bin/python3") == "cp312"


def test_returns_none_on_nonzero_returncode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        MagicMock(return_value=MagicMock(returncode=1, stdout="", stderr="boom")),
    )
    assert abi_probe.probe_python_abi("/usr/bin/python3") is None


def test_returns_none_on_subprocess_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        MagicMock(side_effect=OSError("no such executable")),
    )
    assert abi_probe.probe_python_abi("/usr/bin/python3") is None


def test_returns_none_when_stdout_is_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        MagicMock(return_value=MagicMock(returncode=0, stdout="\n", stderr="")),
    )
    assert abi_probe.probe_python_abi("/usr/bin/python3") is None


def test_accepts_versioned_python_names(monkeypatch: pytest.MonkeyPatch) -> None:
    called = MagicMock(return_value=MagicMock(returncode=0, stdout="cp311\n", stderr=""))
    monkeypatch.setattr(subprocess, "run", called)
    assert abi_probe.probe_python_abi("/usr/bin/python3.11") == "cp311"
    assert called.call_count == 1
