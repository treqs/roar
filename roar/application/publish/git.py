"""Shared git policy helpers for publish workflows."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from ...core.interfaces.logger import ILogger
from ...core.interfaces.registration import GitContext
from ...integrations.git import resolve_git_context
from ...plugins.vcs.git import GitVCSProvider


@dataclass(frozen=True)
class PublishGitState:
    """Resolved git state for a publish workflow."""

    repo_root: Path
    commit: str


def ensure_clean_publish_repo(path: Path, *, error_message: str) -> PublishGitState:
    """Resolve repo state and fail if the working tree is dirty."""
    state = resolve_publish_git_state(path)
    vcs = GitVCSProvider()
    clean, _changes = vcs.get_status(str(state.repo_root))
    if not clean:
        raise ValueError(error_message)
    return state


def resolve_publish_git_state(path: Path) -> PublishGitState:
    """Resolve the repo root and current commit for a publish workflow."""
    vcs = GitVCSProvider()
    repo_root = vcs.get_repo_root(str(path))
    if not repo_root:
        raise ValueError(f"Not a git repository: {path}")

    commit = vcs.get_commit_hash(repo_root)
    if not commit:
        raise ValueError(f"Unable to resolve git commit for repository: {repo_root}")

    return PublishGitState(repo_root=Path(repo_root), commit=commit)


def resolve_publish_git_context(
    path: Path,
    *,
    logger: ILogger,
    git_commit: str | None = None,
) -> GitContext:
    """Resolve git context for publish workflows using the shared transfer helper."""
    logger.debug("Resolving git context from %s", path)
    ctx = resolve_git_context(path, git_commit)
    logger.debug(
        "Git context resolved: repo=%s, commit=%s, branch=%s",
        ctx.repo,
        ctx.commit[:12] if ctx.commit else None,
        ctx.branch,
    )
    return ctx


def build_publish_tag_name(commit: str, *, short: bool = False) -> str:
    """Build the canonical roar publish tag name."""
    normalized_commit = commit[:8] if short else commit
    return f"roar/{normalized_commit}"


def create_publish_git_tag(path: Path, tag_name: str) -> tuple[bool, str | None]:
    """Create a publish tag if it does not already exist."""
    state = resolve_publish_git_state(path)
    if _tag_exists(state.repo_root, tag_name):
        return True, None
    vcs = GitVCSProvider()
    return vcs.create_tag(str(state.repo_root), tag_name)


def _tag_exists(repo_root: Path, tag_name: str) -> bool:
    result = subprocess.run(
        ["git", "tag", "-l", tag_name],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    return tag_name in result.stdout.splitlines()
