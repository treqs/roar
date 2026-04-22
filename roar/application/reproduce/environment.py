"""Environment-preparation helpers for reproduction workflows."""

from __future__ import annotations

import subprocess
from pathlib import Path

from ...core.interfaces.reproduction import EnvironmentInfo, PipelineInfo
from ...execution.reproduction.environment_setup import EnvironmentSetupService
from ...utils.git_url import urls_match


def prepare_reproduction_environment(
    *,
    pipeline: PipelineInfo,
    cwd: Path,
    presenter,
    auto_confirm: bool,
    dpkg_any_version: bool = False,
    pip_any_version: bool = False,
    package_sync: bool = False,
) -> EnvironmentInfo:
    """Reuse the current repo when possible, otherwise create a fresh environment."""
    reused = try_reuse_current_repo(cwd, pipeline, presenter=presenter)
    if reused is not None:
        return reused

    target_dir = cwd / "reproduce"
    presenter.print(f"\nSetting up environment in {target_dir}...")
    return EnvironmentSetupService(presenter=presenter).setup(
        pipeline,
        target_dir,
        auto_confirm,
        dpkg_any_version=dpkg_any_version,
        pip_any_version=pip_any_version,
        package_sync=package_sync,
    )


def try_reuse_current_repo(
    cwd: Path,
    pipeline: PipelineInfo,
    *,
    presenter,
) -> EnvironmentInfo | None:
    """Reuse the current git checkout when its remote matches the pipeline repo."""
    try:
        repo_root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            cwd=cwd,
            check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None

    try:
        origin_url = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            cwd=repo_root,
            check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None

    if not pipeline.git_repo or not urls_match(origin_url, pipeline.git_repo):
        return None

    presenter.print("Current repository matches recorded remote, using existing environment")

    repo_dir = Path(repo_root)
    if pipeline.git_commit:
        try:
            subprocess.run(
                ["git", "checkout", pipeline.git_commit],
                capture_output=True,
                text=True,
                cwd=repo_root,
                check=True,
            )
        except subprocess.CalledProcessError:
            presenter.print(
                f"Warning: could not checkout commit {pipeline.git_commit}, "
                "continuing with current HEAD"
            )

    venv_dir = repo_dir / ".venv" if (repo_dir / ".venv").is_dir() else None
    return EnvironmentInfo(repo_dir=repo_dir, venv_dir=venv_dir, python_version=None)
