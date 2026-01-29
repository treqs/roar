"""
Shared pytest fixtures for roar tests.

This module provides fixtures for integration testing the roar CLI:
- temp_git_repo: Creates an isolated git repository with roar initialized
- roar_cli: Helper to run roar CLI commands via subprocess
- git_commit: Helper to commit changes between steps
"""

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest


def _run_roar_cmd(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    """Run a roar command using the current Python interpreter."""
    result = subprocess.run(
        [sys.executable, "-m", "roar", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode,
            [sys.executable, "-m", "roar", *args],
            result.stdout,
            result.stderr,
        )
    return result


@pytest.fixture
def temp_git_repo(tmp_path: Path) -> Path:
    """
    Create a temporary git repository with roar initialized.

    Sets up:
    - Empty git repository with initial commit
    - .roar directory via `roar init`
    - .gitignore with .roar/ entry

    Returns:
        Path to the temporary repository root
    """
    # Initialize git repo
    subprocess.run(
        ["git", "init"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
    )

    # Configure git user for commits
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
    )

    # Create .gitignore
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text(".roar/\n")

    # Initial commit
    subprocess.run(
        ["git", "add", ".gitignore"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
    )

    # Initialize roar (use -y to auto-accept gitignore)
    _run_roar_cmd("init", "-y", cwd=tmp_path)

    # Disable ignore_tmp_files since tests run in /tmp directories
    config_path = tmp_path / ".roar" / "config.toml"
    config_content = config_path.read_text()
    config_content = config_content.replace("ignore_tmp_files = true", "ignore_tmp_files = false")
    config_path.write_text(config_content)

    return tmp_path


@pytest.fixture
def roar_cli(temp_git_repo: Path) -> Callable[..., subprocess.CompletedProcess]:
    """
    Provide a helper function to run roar CLI commands.

    Args:
        temp_git_repo: The temporary repository path

    Returns:
        A callable that runs roar commands and returns CompletedProcess
    """

    def run_roar(*args: str, check: bool = True) -> subprocess.CompletedProcess:
        """
        Run a roar command.

        Args:
            *args: Arguments to pass to roar (e.g., "run", "python", "script.py")
            check: Whether to raise on non-zero exit code

        Returns:
            CompletedProcess with stdout/stderr as strings
        """
        return _run_roar_cmd(*args, cwd=temp_git_repo, check=check)

    return run_roar


@pytest.fixture
def git_commit(temp_git_repo: Path) -> Callable[[str], None]:
    """
    Provide a helper function to commit all changes.

    The roar run command requires a clean git working tree,
    so this fixture is used between steps to commit changes.

    Args:
        temp_git_repo: The temporary repository path

    Returns:
        A callable that stages and commits all changes
    """

    def commit(message: str = "Update") -> None:
        """
        Stage and commit all changes.

        Args:
            message: Commit message
        """
        subprocess.run(
            ["git", "add", "-A"],
            cwd=temp_git_repo,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", message, "--allow-empty"],
            cwd=temp_git_repo,
            capture_output=True,
            check=True,
        )

    return commit
