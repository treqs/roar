"""Localized tests for the pytest subprocess harness."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import Mock

import tests.conftest as test_conftest


def test_subprocess_env_prepends_repo_local_binary_dirs(monkeypatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setattr(
        test_conftest,
        "_repo_local_binary_dirs",
        lambda: ["/tmp/release-bin", "/tmp/package-bin"],
    )

    env = test_conftest._subprocess_env()

    assert env["PATH"].split(os.pathsep)[:3] == ["/tmp/release-bin", "/tmp/package-bin", "/usr/bin"]


def test_release_artifacts_missing_or_stale_detects_missing_output(tmp_path: Path) -> None:
    assert test_conftest._release_artifacts_missing_or_stale([tmp_path / "roar-tracer"])


def test_release_artifacts_missing_or_stale_compares_source_and_output_mtime(
    monkeypatch, tmp_path: Path
) -> None:
    source = tmp_path / "main.rs"
    output = tmp_path / "roar-tracer"
    source.write_text("source")
    output.write_text("binary")
    monkeypatch.setattr(test_conftest, "_rust_source_inputs", lambda: [source])

    os.utime(source, (20, 20))
    os.utime(output, (10, 10))
    assert test_conftest._release_artifacts_missing_or_stale([output])

    os.utime(output, (30, 30))
    assert not test_conftest._release_artifacts_missing_or_stale([output])


def test_run_command_ensures_repo_local_ptrace_tracer(monkeypatch, tmp_path: Path) -> None:
    ensure_tracer = Mock()
    monkeypatch.setattr(test_conftest, "_ensure_repo_local_ptrace_tracer", ensure_tracer)

    completed = subprocess.CompletedProcess(
        args=["python", "-m", "roar", "run", "python", "script.py"],
        returncode=0,
        stdout="",
        stderr="",
    )
    monkeypatch.setattr(test_conftest.subprocess, "run", lambda *args, **kwargs: completed)

    result = test_conftest._run_roar_cmd("run", "python", "script.py", cwd=tmp_path)

    assert result is completed
    ensure_tracer.assert_called_once_with()


def test_non_run_command_skips_repo_local_ptrace_build(monkeypatch, tmp_path: Path) -> None:
    ensure_tracer = Mock()
    monkeypatch.setattr(test_conftest, "_ensure_repo_local_ptrace_tracer", ensure_tracer)

    completed = subprocess.CompletedProcess(
        args=["python", "-m", "roar", "config", "list"],
        returncode=0,
        stdout="",
        stderr="",
    )
    monkeypatch.setattr(test_conftest.subprocess, "run", lambda *args, **kwargs: completed)

    result = test_conftest._run_roar_cmd("config", "list", cwd=tmp_path)

    assert result is completed
    ensure_tracer.assert_not_called()
