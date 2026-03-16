"""
Shared pytest fixtures for roar tests.

This module provides fixtures for integration testing the roar CLI:
- temp_git_repo: Creates an isolated git repository with roar initialized
- roar_cli: Helper to run roar CLI commands via subprocess
- git_commit: Helper to commit changes between steps
"""

import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - non-Unix fallback
    fcntl = None

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RUST_MANIFEST = REPO_ROOT / "rust" / "Cargo.toml"
RELEASE_BIN_DIR = REPO_ROOT / "rust" / "target" / "release"
PACKAGE_BIN_DIR = REPO_ROOT / "roar" / "bin"


def _repo_local_binary_dirs() -> list[str]:
    dirs: list[str] = []
    for directory in (RELEASE_BIN_DIR, PACKAGE_BIN_DIR):
        if directory.is_dir():
            dirs.append(str(directory))
    return dirs


def _repo_local_ptrace_exists() -> bool:
    return (RELEASE_BIN_DIR / "roar-tracer").exists() or (PACKAGE_BIN_DIR / "roar-tracer").exists()


@lru_cache(maxsize=1)
def _ensure_repo_local_ptrace_tracer() -> None:
    if not sys.platform.startswith("linux"):
        return
    if _repo_local_ptrace_exists():
        return

    cargo = shutil.which("cargo")
    if cargo is None:
        raise RuntimeError(
            "Integration tests need a repo-local roar-tracer, but `cargo` was not found on PATH."
        )

    lock_path = Path(tempfile.gettempdir()) / "roar-test-suite-optimizations-roar-tracer.lock"
    with lock_path.open("w", encoding="utf-8") as lock_file:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            if _repo_local_ptrace_exists():
                return
            result = subprocess.run(
                [
                    cargo,
                    "build",
                    "--release",
                    "--manifest-path",
                    str(RUST_MANIFEST),
                    "-p",
                    "roar-tracer",
                ],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                env=dict(os.environ),
            )
            if result.returncode != 0:
                raise RuntimeError(
                    "Failed to build a repo-local roar-tracer for integration tests.\n"
                    f"stdout:\n{result.stdout or '<empty>'}\n"
                    f"stderr:\n{result.stderr or '<empty>'}"
                )
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _subprocess_env() -> dict[str, str]:
    """Ensure subprocess CLI calls import the current worktree first."""
    env = dict(os.environ)
    repo_root = str(REPO_ROOT)
    current_pythonpath = env.get("PYTHONPATH", "")
    pythonpath_entries = current_pythonpath.split(os.pathsep) if current_pythonpath else []
    if repo_root not in pythonpath_entries:
        env["PYTHONPATH"] = (
            f"{repo_root}{os.pathsep}{current_pythonpath}" if current_pythonpath else repo_root
        )
    repo_binary_dirs = _repo_local_binary_dirs()
    current_path = env.get("PATH", "")
    path_entries = current_path.split(os.pathsep) if current_path else []
    new_entries = [entry for entry in repo_binary_dirs if entry not in path_entries]
    if new_entries:
        env["PATH"] = os.pathsep.join([*new_entries, *path_entries]) if path_entries else os.pathsep.join(new_entries)
    return env


os.environ["PYTHONPATH"] = _subprocess_env()["PYTHONPATH"]
os.environ["PATH"] = _subprocess_env()["PATH"]


def _run_roar_cmd(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    """Run a roar command using the current Python interpreter."""
    if args and args[0] in {"run", "build"}:
        _ensure_repo_local_ptrace_tracer()
    command = [sys.executable, "-m", "roar", *args]
    result = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        env=_subprocess_env(),
    )
    if check and result.returncode != 0:
        stdout = result.stdout or "<empty>"
        stderr = result.stderr or "<empty>"
        command_with_output = (
            f"{' '.join(command)}\n--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}"
        )
        raise subprocess.CalledProcessError(
            result.returncode,
            command_with_output,
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
