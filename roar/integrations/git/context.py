"""Git context resolution helpers for application workflows."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...core.interfaces.registration import GitContext
from ...core.logging import get_logger
from .provider import GitVCSProvider


def resolve_repo_url_or_local_uri(
    vcs: GitVCSProvider,
    repo_root: str,
    logger: Any | None = None,
    configured_remote: str | None = None,
) -> str:
    """Resolve remote URL, falling back to local file:// repo URI.

    ``configured_remote`` (from ``git.remote``) is preferred when set; otherwise
    the canonical remote is resolved (origin, else the sole remote) rather than
    assuming ``origin`` — so a repo whose only remote is named e.g. ``treqs``
    records that URL instead of a local ``file://`` path.
    """
    remote_url = vcs.get_remote_url(repo_root, configured=configured_remote)
    if remote_url:
        return remote_url

    fallback_uri = Path(repo_root).resolve().as_uri()
    active_logger = logger or get_logger()
    active_logger.warning(
        "No git remote configured for %s; using local repository URI %s for session registration",
        repo_root,
        fallback_uri,
    )
    return fallback_uri


def resolve_git_context(
    repo_root: Path,
    git_commit: str | None = None,
    configured_remote: str | None = None,
) -> GitContext:
    """Resolve git context from a repository path."""
    try:
        vcs = GitVCSProvider()
        root = vcs.get_repo_root(str(repo_root))
        if not root:
            return GitContext(repo=None, commit=git_commit, branch=None)

        return GitContext(
            repo=resolve_repo_url_or_local_uri(vcs, root, configured_remote=configured_remote),
            commit=git_commit or vcs.get_commit_hash(root),
            branch=vcs.get_branch(root),
        )
    except (OSError, RuntimeError, ValueError) as exc:
        get_logger().debug("Failed to resolve git context at %s: %s", repo_root, exc)
        return GitContext(repo=None, commit=git_commit, branch=None)
