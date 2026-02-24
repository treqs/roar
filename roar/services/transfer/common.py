"""Shared transfer-layer utilities for get/put services."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

from ...core.interfaces.registration import GitContext
from ...core.logging import get_logger
from ...db.hashing.backend import compute_hashes_batch
from ...plugins.vcs.git import GitVCSProvider


class DatabaseContext(Protocol):
    """Protocol for database context dependency."""

    @property
    def artifacts(self) -> Any: ...

    @property
    def jobs(self) -> Any: ...

    @property
    def sessions(self) -> Any: ...


def resolve_repo_url_or_local_uri(
    vcs: GitVCSProvider,
    repo_root: str,
    logger: Any | None = None,
) -> str:
    """Resolve remote URL, falling back to local file:// repo URI."""
    remote_url = vcs.get_remote_url(repo_root)
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


def hash_files_blake3(paths: list[Path]) -> dict[str, str]:
    """Compute BLAKE3 hashes for paths in one backend batch call."""
    if not paths:
        return {}

    raw_result = compute_hashes_batch(paths, ["blake3"])
    result: dict[str, str] = {}
    for path in paths:
        key = str(path)
        digest = raw_result.get(key, {}).get("blake3")
        if digest:
            result[key] = digest
    return result


def resolve_git_context(repo_root: Path, git_commit: str | None = None) -> GitContext:
    """Resolve git context from a repository path."""
    try:
        vcs = GitVCSProvider()
        root = vcs.get_repo_root(str(repo_root))
        if not root:
            return GitContext(repo=None, commit=git_commit, branch=None)

        return GitContext(
            repo=resolve_repo_url_or_local_uri(vcs, root),
            commit=git_commit or vcs.get_commit_hash(root),
            branch=vcs.get_branch(root),
        )
    except (OSError, RuntimeError, ValueError) as e:
        get_logger().debug("Failed to resolve git context at %s: %s", repo_root, e)
        return GitContext(repo=None, commit=git_commit, branch=None)


def build_operation_metadata_json(operation: str, payload: dict[str, Any]) -> str:
    """Wrap operation payload in a namespaced metadata object and serialize to JSON."""
    return json.dumps({operation: payload})
