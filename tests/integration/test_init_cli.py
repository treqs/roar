"""Product-path coverage for the `roar init` CLI."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


def _run_roar_init(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "roar", "init", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=dict(os.environ),
    )


def _active_session_row(repo_root: Path) -> sqlite3.Row | None:
    db_path = repo_root / ".roar" / "roar.db"
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT id, is_active, current_step FROM sessions WHERE is_active = 1 ORDER BY id DESC LIMIT 1"
        ).fetchone()


def test_init_with_yes_adds_roar_to_gitignore(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    gitignore_path = tmp_path / ".gitignore"
    gitignore_path.write_text("# test ignore rules\n")

    result = _run_roar_init("-y", cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert (tmp_path / ".roar").is_dir()
    assert (tmp_path / ".roar" / "roar.db").is_file()
    assert (tmp_path / ".roar" / "config.toml").is_file()
    config_text = (tmp_path / ".roar" / "config.toml").read_text(encoding="utf-8")
    # Scope is unset at init now (resolves at publish time), so no baked mode.
    assert 'mode = "anonymous"' not in config_text
    active_session = _active_session_row(tmp_path)
    assert active_session is not None
    assert active_session["is_active"] == 1
    assert active_session["current_step"] == 1
    assert "added .roar/ entry" in result.stdout
    assert "scope:      anonymous (public; no account)" in result.stdout
    assert gitignore_path.read_text().endswith(".roar/\n")


def test_init_with_no_preserves_gitignore(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    gitignore_path = tmp_path / ".gitignore"
    original_gitignore = "# ignore build artifacts\n"
    gitignore_path.write_text(original_gitignore)

    result = _run_roar_init("-n", cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert (tmp_path / ".roar").is_dir()
    active_session = _active_session_row(tmp_path)
    assert active_session is not None
    assert active_session["is_active"] == 1
    assert "skipped (--no-gitignore)" in result.stdout
    assert gitignore_path.read_text() == original_gitignore


def test_init_with_path_initializes_target_directory(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    result = _run_roar_init("--path", str(project_dir), "-n", cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert (project_dir / ".roar").is_dir()
    assert (project_dir / ".roar" / "roar.db").is_file()
    assert (project_dir / ".roar" / "config.toml").is_file()
    active_session = _active_session_row(project_dir)
    assert active_session is not None
    assert active_session["is_active"] == 1
    assert not (tmp_path / ".roar").exists()
    assert "Initialized roar in" in result.stdout
