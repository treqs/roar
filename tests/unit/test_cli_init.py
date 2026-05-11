from __future__ import annotations

import os
import subprocess
from pathlib import Path

from click.testing import CliRunner

from roar.cli import cli


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)


def _run_init(repo: Path, *args: str) -> object:
    runner = CliRunner()
    original_cwd = Path.cwd()
    try:
        os.chdir(repo)
        return runner.invoke(cli, ["init", *args])
    finally:
        os.chdir(original_cwd)


def test_init_creates_gitignore_when_missing(tmp_path: Path) -> None:
    """P0-8: with no .gitignore, init must create one. Otherwise the .roar/
    dir it just made dirties the worktree and the next `roar run` refuses."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    gitignore = repo / ".gitignore"
    assert not gitignore.exists()

    result = _run_init(repo)

    assert result.exit_code == 0, result.output
    assert gitignore.exists()
    assert ".roar/" in gitignore.read_text().splitlines()
    assert "created .gitignore with .roar/ entry" in result.output


def test_init_appends_to_existing_gitignore_without_roar(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    gitignore = repo / ".gitignore"
    gitignore.write_text("__pycache__/\n*.pyc\n")

    result = _run_init(repo)

    assert result.exit_code == 0, result.output
    lines = gitignore.read_text().splitlines()
    assert "__pycache__/" in lines
    assert "*.pyc" in lines
    assert ".roar/" in lines
    assert "added .roar/ entry" in result.output


def test_init_idempotent_when_gitignore_already_has_roar_slash(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    gitignore = repo / ".gitignore"
    gitignore.write_text("foo\n.roar/\nbar\n")

    result = _run_init(repo)

    assert result.exit_code == 0, result.output
    assert gitignore.read_text() == "foo\n.roar/\nbar\n"
    assert "already excluded" in result.output


def test_init_idempotent_when_gitignore_already_has_roar_unslashed(tmp_path: Path) -> None:
    """`.roar` (no slash) is also a valid gitignore entry covering the dir."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    gitignore = repo / ".gitignore"
    gitignore.write_text(".roar\n")

    result = _run_init(repo)

    assert result.exit_code == 0, result.output
    assert gitignore.read_text() == ".roar\n"
    assert "already excluded" in result.output


def test_init_no_gitignore_flag_skips(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    result = _run_init(repo, "--no-gitignore")

    assert result.exit_code == 0, result.output
    assert not (repo / ".gitignore").exists()
    assert "skipped (--no-gitignore)" in result.output


def test_init_short_no_flag_still_skips(tmp_path: Path) -> None:
    """Back-compat: -n / --no remain aliases for --no-gitignore."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    result = _run_init(repo, "-n")

    assert result.exit_code == 0, result.output
    assert not (repo / ".gitignore").exists()


def test_init_substring_match_does_not_count_as_already_present(tmp_path: Path) -> None:
    """A .gitignore entry like `not.roar` shares a substring with `.roar`
    but is a different path. The check must be per-line, not substring."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    gitignore = repo / ".gitignore"
    gitignore.write_text("not.roar\n")

    result = _run_init(repo)

    assert result.exit_code == 0, result.output
    lines = gitignore.read_text().splitlines()
    assert "not.roar" in lines
    assert ".roar/" in lines
    assert "added .roar/ entry" in result.output


def test_init_appends_when_gitignore_missing_trailing_newline(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    gitignore = repo / ".gitignore"
    gitignore.write_text("foo")  # no trailing newline

    result = _run_init(repo)

    assert result.exit_code == 0, result.output
    assert gitignore.read_text() == "foo\n.roar/\n"


def test_init_outside_git_repo_does_not_touch_gitignore(tmp_path: Path) -> None:
    """If we're not inside a git repo, there's no shared root to write to."""
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()

    result = _run_init(plain_dir)

    assert result.exit_code == 0, result.output
    assert not (plain_dir / ".gitignore").exists()
    assert "(not in a git repo)" in result.output


def test_init_hints_visible_by_default(tmp_path: Path) -> None:
    """Git-style `hint:` lines appear after the summary block."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    result = _run_init(repo)

    assert result.exit_code == 0, result.output
    assert "hint: Get started:" in result.output
    assert "roar run python train.py" in result.output
    assert "roar dag" in result.output
    assert "roar register" in result.output
    assert "https://glaas.ai/docs" in result.output
    assert "roar config set advice.enabled false" in result.output


def test_init_hints_suppressed_when_advice_disabled(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    (repo / ".roar").mkdir()
    (repo / ".roar" / "config.toml").write_text("[advice]\nenabled = false\n")

    result = _run_init(repo)

    assert result.exit_code == 0, result.output
    # Summary block still appears.
    assert (
        "Initialized roar in" in result.output or ".roar directory already exists" in result.output
    )
    # No hints.
    assert "hint:" not in result.output


def test_init_path_uses_target_repo_for_gitignore_updates(tmp_path: Path) -> None:
    caller_repo = tmp_path / "caller-repo"
    target_repo = tmp_path / "target-repo"
    caller_repo.mkdir()
    target_repo.mkdir()

    _init_git_repo(caller_repo)
    _init_git_repo(target_repo)

    caller_gitignore = caller_repo / ".gitignore"
    target_gitignore = target_repo / ".gitignore"
    caller_gitignore.write_text(".roar/\n")
    target_gitignore.write_text("")

    runner = CliRunner()
    original_cwd = Path.cwd()
    try:
        os.chdir(caller_repo)
        result = runner.invoke(cli, ["init", "--path", str(target_repo), "-y"])
    finally:
        os.chdir(original_cwd)

    assert result.exit_code == 0, result.output
    assert "added .roar/ entry" in result.output
    assert "already excluded" not in result.output
    assert caller_gitignore.read_text() == ".roar/\n"
    assert ".roar/" in target_gitignore.read_text().splitlines()
    assert (target_repo / ".roar").is_dir()
