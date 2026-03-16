"""Product-path coverage for the `roar tracer` CLI."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import tests.conftest as test_conftest

pytestmark = pytest.mark.integration


def _run_roar_tracer(
    *args: str,
    cwd: Path,
    env_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-m", "roar", "tracer", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env,
    )


def _write_executable(path: Path, body: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path.resolve()


def _repo_local_ptrace_binary() -> str | None:
    for candidate in (
        test_conftest.RELEASE_BIN_DIR / "roar-tracer",
        test_conftest.PACKAGE_BIN_DIR / "roar-tracer",
    ):
        if candidate.exists():
            return str(candidate.resolve())
    return None


def test_tracer_status_reports_configured_default_and_repo_local_ptrace_binary(
    temp_git_repo: Path,
    roar_cli,
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "roar-tracer")
    expected_ptrace = _repo_local_ptrace_binary()

    set_result = roar_cli("tracer", "ptrace")
    assert set_result.returncode == 0

    result = _run_roar_tracer(
        "status",
        cwd=temp_git_repo,
        env_overrides={"PATH": str(bin_dir)},
    )

    assert result.returncode == 0, result.stderr
    assert "Default tracer: ptrace" in result.stdout
    assert "Fallback enabled: True" in result.stdout
    assert "Proxy enabled: False" in result.stdout
    if expected_ptrace is not None:
        assert f"ptrace:  {expected_ptrace}" in result.stdout
    else:
        assert "ptrace:  not found" in result.stdout
    assert "  ebpf:" in result.stdout
    assert "  preload:" in result.stdout
    assert "  roard:" in result.stdout


def test_tracer_check_uses_configured_default_backend_and_repo_local_binary(
    temp_git_repo: Path,
    roar_cli,
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "roar-tracer")
    expected_ptrace = _repo_local_ptrace_binary()
    if expected_ptrace is None:
        pytest.skip("repo-local ptrace tracer is only expected on Linux test environments")

    roar_cli("tracer", "ptrace")

    result = _run_roar_tracer(
        "check",
        cwd=temp_git_repo,
        env_overrides={"PATH": str(bin_dir)},
    )

    assert result.returncode == 0, result.stderr
    assert f"Tracer check passed for 'ptrace': {expected_ptrace}" in result.stdout


def test_tracer_check_prefers_repo_local_binary_over_path_override(
    temp_git_repo: Path,
    roar_cli,
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "roar-tracer")
    expected_ptrace = _repo_local_ptrace_binary()
    if expected_ptrace is None:
        pytest.skip("repo-local ptrace tracer is only expected on Linux test environments")

    roar_cli("tracer", "ptrace")

    result = _run_roar_tracer(
        "check",
        "--backend",
        "ptrace",
        cwd=temp_git_repo,
        env_overrides={"PATH": str(bin_dir)},
    )

    assert result.returncode == 0, result.stderr
    assert f"Tracer check passed for 'ptrace': {expected_ptrace}" in result.stdout


def test_tracer_setup_without_subcommand_shows_help(temp_git_repo: Path) -> None:
    result = _run_roar_tracer("setup", cwd=temp_git_repo)

    assert result.returncode == 0, result.stderr
    assert "Set up tracer backends." in result.stdout
    assert "ebpf" in result.stdout
