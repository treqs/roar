"""Regression test: roar init must detect git repo even before bootstrap."""

import os
import subprocess
from pathlib import Path

from click.testing import CliRunner

from roar.cli import cli


def test_init_detects_git_repo(tmp_path: Path) -> None:
    """roar init -y should not print 'Not in a git repository' inside a git repo.

    This is a regression test for a bug where RoarContext._get_repo_root()
    returned None when the container wasn't bootstrapped yet, causing init
    to skip .gitignore setup.
    """
    # Set up a git repo with a .gitignore
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / ".gitignore").write_text("")

    runner = CliRunner()
    orig_dir = os.getcwd()
    try:
        os.chdir(tmp_path)
        result = runner.invoke(cli, ["init", "-y"])
    finally:
        os.chdir(orig_dir)

    assert "Not in a git repository" not in result.output, (
        f"init failed to detect git repo. Output:\n{result.output}"
    )
