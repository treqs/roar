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


def _rust_source_inputs() -> list[Path]:
    inputs: list[Path] = []
    for pattern in ("**/*.rs", "**/Cargo.toml", "Cargo.lock"):
        inputs.extend(
            path for path in RUST_MANIFEST.parent.glob(pattern) if "target" not in path.parts
        )
    return inputs


def _release_artifacts_missing_or_stale(outputs: list[Path]) -> bool:
    if not outputs or any(not output.exists() for output in outputs):
        return True

    newest_source_mtime = max((path.stat().st_mtime for path in _rust_source_inputs()), default=0.0)
    oldest_output_mtime = min(output.stat().st_mtime for output in outputs)
    return newest_source_mtime > oldest_output_mtime


def _run_cargo_release_build(*packages: str, lock_name: str) -> None:
    cargo = shutil.which("cargo")
    if cargo is None:
        raise RuntimeError(
            "Integration tests need repo-local tracer artifacts, but `cargo` was not found on PATH."
        )

    lock_path = Path(tempfile.gettempdir()) / lock_name
    with lock_path.open("w", encoding="utf-8") as lock_file:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            command = [cargo, "build", "--release", "--manifest-path", str(RUST_MANIFEST)]
            for package in packages:
                command.extend(["-p", package])
            result = subprocess.run(
                command,
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                env=dict(os.environ),
            )
            if result.returncode != 0:
                raise RuntimeError(
                    "Failed to build repo-local tracer artifacts for integration tests.\n"
                    f"stdout:\n{result.stdout or '<empty>'}\n"
                    f"stderr:\n{result.stderr or '<empty>'}"
                )
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _repo_local_binary_dirs() -> list[str]:
    dirs: list[str] = []
    for directory in (RELEASE_BIN_DIR, PACKAGE_BIN_DIR):
        if directory.is_dir():
            dirs.append(str(directory))
    return dirs


def _repo_local_ptrace_exists() -> bool:
    return (RELEASE_BIN_DIR / "roar-tracer").exists() or (PACKAGE_BIN_DIR / "roar-tracer").exists()


def _preload_library_path() -> Path | None:
    for name in ("libroar_tracer_preload.so", "libroar-tracer-preload.so"):
        candidate = RELEASE_BIN_DIR / name
        if candidate.exists():
            return candidate
    return None


@lru_cache(maxsize=1)
def _ensure_repo_local_ptrace_tracer() -> None:
    if not sys.platform.startswith("linux"):
        return
    release_binary = RELEASE_BIN_DIR / "roar-tracer"
    if not _release_artifacts_missing_or_stale([release_binary]):
        return

    _run_cargo_release_build(
        "roar-tracer",
        lock_name="roar-test-suite-optimizations-roar-tracer.lock",
    )
    if _release_artifacts_missing_or_stale([release_binary]):
        raise RuntimeError("cargo build completed but rust/target/release/roar-tracer is stale")


@lru_cache(maxsize=1)
def _ensure_repo_local_preload_tracer() -> Path:
    if not sys.platform.startswith("linux"):
        pytest.skip("preload tracer requires Linux")

    launcher = RELEASE_BIN_DIR / "roar-tracer-preload"
    library = _preload_library_path()
    outputs = [launcher, library] if library is not None else [launcher]
    if _release_artifacts_missing_or_stale(outputs):
        _run_cargo_release_build(
            "roar-tracer-preload",
            lock_name="roar-test-suite-optimizations-roar-tracer-preload.lock",
        )
        library = _preload_library_path()

    if library is None or _release_artifacts_missing_or_stale([launcher, library]):
        raise RuntimeError(
            "cargo build completed but release preload tracer artifacts are missing or stale"
        )
    return launcher


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
        env["PATH"] = (
            os.pathsep.join([*new_entries, *path_entries])
            if path_entries
            else os.pathsep.join(new_entries)
        )
    return env


os.environ["PYTHONPATH"] = _subprocess_env()["PYTHONPATH"]
os.environ["PATH"] = _subprocess_env()["PATH"]


def _run_roar_cmd(
    *args: str,
    cwd: Path,
    check: bool = True,
    env_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run a roar command using the current Python interpreter."""
    if args and args[0] in {"run", "build"}:
        _ensure_repo_local_ptrace_tracer()
    command = [sys.executable, "-m", "roar", *args]
    env = _subprocess_env()
    if env_overrides:
        env.update(env_overrides)
    result = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env,
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


@pytest.fixture(scope="session")
def _git_repo_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """
    Session-scoped canonical initialized repo used as a copy-source by temp_git_repo.

    Built once per xdist worker (or once for the whole session in non-parallel runs).
    Each call to temp_git_repo gets a fresh shutil.copytree() of this template, which
    takes ~50ms instead of the ~35s that running git-init + roar-init subprocesses costs.
    """
    template = tmp_path_factory.mktemp("_repo_template")

    subprocess.run(["git", "init"], cwd=template, capture_output=True, check=True)

    # Write user config directly to avoid two extra subprocess round-trips
    git_config = template / ".git" / "config"
    git_config.write_text(
        git_config.read_text() + "\n[user]\n\temail = test@example.com\n\tname = Test User\n"
    )

    (template / ".gitignore").write_text(".roar/\n")
    subprocess.run(["git", "add", ".gitignore"], cwd=template, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"], cwd=template, capture_output=True, check=True
    )

    _run_roar_cmd("init", "-y", cwd=template)

    config_path = template / ".roar" / "config.toml"
    config_path.write_text(
        config_path.read_text().replace("ignore_tmp_files = true", "ignore_tmp_files = false")
    )

    return template


@pytest.fixture
def temp_git_repo(tmp_path: Path, _git_repo_template: Path) -> Path:
    """
    Create a temporary git repository with roar initialized.

    Copies a session-scoped pre-built template rather than running git-init and
    roar-init subprocesses for every test, reducing per-test setup from ~35s to ~50ms.
    """
    shutil.copytree(_git_repo_template, tmp_path, dirs_exist_ok=True)
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

    def run_roar(
        *args: str,
        check: bool = True,
        env_overrides: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess:
        """
        Run a roar command.

        Args:
            *args: Arguments to pass to roar (e.g., "run", "python", "script.py")
            check: Whether to raise on non-zero exit code
            env_overrides: Extra environment variables for this invocation

        Returns:
            CompletedProcess with stdout/stderr as strings
        """
        return _run_roar_cmd(*args, cwd=temp_git_repo, check=check, env_overrides=env_overrides)

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
