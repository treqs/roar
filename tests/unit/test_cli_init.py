from __future__ import annotations

import os
import subprocess
from pathlib import Path

from click.testing import CliRunner

from roar.cli import cli


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)


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
    assert "Added .roar/ to .gitignore" in result.output
    assert ".roar is already in .gitignore" not in result.output
    assert caller_gitignore.read_text() == ".roar/\n"
    assert ".roar/" in target_gitignore.read_text().splitlines()
    assert (target_repo / ".roar").is_dir()
