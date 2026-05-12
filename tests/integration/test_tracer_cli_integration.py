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


def _expected_ptrace_binary(path_bin_dir: Path | None = None) -> str:
    repo_local = _repo_local_ptrace_binary()
    if repo_local is not None:
        return repo_local
    assert path_bin_dir is not None
    return str((path_bin_dir / "roar-tracer").resolve())


def test_tracer_status_shows_active_line_and_tradeoff_table(
    temp_git_repo: Path,
    roar_cli,
    tmp_path: Path,
) -> None:
    """`roar tracer` (no args) leads with the brand banner, then the
    Active line, then a 3-row tradeoff table. Paths live in `check`,
    not here."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "roar-tracer")

    set_result = roar_cli("tracer", "use", "ptrace")
    assert set_result.returncode == 0, set_result.stderr

    result = _run_roar_tracer(
        "",
        cwd=temp_git_repo,
        env_overrides={"PATH": str(bin_dir)},
    ) if False else _run_roar_tracer(  # noqa: SIM108 — passing zero args
        cwd=temp_git_repo,
        env_overrides={"PATH": str(bin_dir)},
    )

    assert result.returncode == 0, result.stderr
    assert "roar v" in result.stdout  # brand banner
    assert "Active: ptrace" in result.stdout
    # 3-row tradeoff table.
    assert "ebpf" in result.stdout
    assert "preload" in result.stdout
    assert "ptrace" in result.stdout
    assert "fastest, low overhead" in result.stdout
    # Paths are NOT shown on the status home screen.
    assert "/roar-tracer" not in result.stdout


def test_tracer_check_uses_configured_default_backend_and_repo_local_binary(
    temp_git_repo: Path,
    roar_cli,
) -> None:
    expected_ptrace = _repo_local_ptrace_binary()
    if expected_ptrace is None:
        pytest.skip("strict ptrace preflight requires a built repo-local ptrace tracer")

    roar_cli("tracer", "use", "ptrace")

    result = _run_roar_tracer(
        "check",
        cwd=temp_git_repo,
    )

    assert result.returncode == 0, result.stderr
    assert "Tracer check passed for 'ptrace': ptrace preflight succeeded" in result.stdout
    # `check` now surfaces the binary path on its own "  binary:" line.
    assert f"binary: {expected_ptrace}" in result.stdout
    assert f"binary: ok ({expected_ptrace})" in result.stdout


def test_tracer_check_prefers_repo_local_binary_over_path_override(
    temp_git_repo: Path,
    roar_cli,
    tmp_path: Path,
) -> None:
    expected_ptrace = _repo_local_ptrace_binary()
    if expected_ptrace is None:
        pytest.skip("strict ptrace preflight requires a built repo-local ptrace tracer")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_path_ptrace = _write_executable(bin_dir / "roar-tracer")

    roar_cli("tracer", "use", "ptrace")

    result = _run_roar_tracer(
        "check",
        "--backend",
        "ptrace",
        cwd=temp_git_repo,
        env_overrides={"PATH": str(bin_dir)},
    )

    assert result.returncode == 0, result.stderr
    assert "Tracer check passed for 'ptrace': ptrace preflight succeeded" in result.stdout
    assert f"binary: ok ({expected_ptrace})" in result.stdout
    assert str(fake_path_ptrace) not in result.stdout


def test_tracer_enable_unknown_backend_rejected(temp_git_repo: Path) -> None:
    """The redesigned surface drops the `setup` group; `enable` only
    accepts `ebpf` today. preload/ptrace need no host setup."""
    result = _run_roar_tracer("enable", "preload", cwd=temp_git_repo)
    assert result.returncode != 0
    assert "Invalid value for" in result.stderr or "Usage" in result.stderr
