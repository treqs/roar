"""Shared git policy helpers for publish workflows."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from ...core.interfaces.logger import ILogger
from ...core.interfaces.registration import GitContext
from ...integrations.git import GitVCSProvider, resolve_git_context


@dataclass(frozen=True)
class PublishGitState:
    """Resolved git state for a publish workflow."""

    repo_root: Path
    commit: str


@dataclass(frozen=True)
class PreparedPutGitState:
    """Prepared git context for a put workflow."""

    git_state: PublishGitState | None
    git_commit: str | None
    expected_tag: str | None
    warnings: tuple[str, ...] = ()


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


def prepare_put_publish_git(
    *,
    repo_root: Path,
    dry_run: bool,
    no_tag: bool,
    logger: ILogger,
) -> PreparedPutGitState:
    """Resolve git preflight state for a put workflow."""
    if dry_run:
        return PreparedPutGitState(
            git_state=None,
            git_commit=None,
            expected_tag=None,
        )

    try:
        git_state = ensure_clean_publish_repo(
            repo_root,
            error_message=(
                "Repository has uncommitted changes.\n"
                "Please commit your changes before running 'roar put'.\n"
                "This ensures artifacts can be traced to a specific commit."
            ),
        )
        expected_tag = None if no_tag else build_publish_tag_name(git_state.commit)
        return PreparedPutGitState(
            git_state=git_state,
            git_commit=git_state.commit,
            expected_tag=expected_tag,
        )
    except ValueError:
        raise
    except Exception as exc:
        logger.debug("Git operation failed during put preflight: %s", exc)
        return PreparedPutGitState(
            git_state=None,
            git_commit=None,
            expected_tag=None,
            warnings=(f"Git operation failed: {exc}",),
        )


def finalize_put_publish_git(
    *,
    result_success: bool,
    result_dry_run: bool,
    no_tag: bool,
    git_commit: str | None,
    expected_tag: str | None,
    git_state: PublishGitState | None,
    repo_root: Path,
    logger: ILogger,
) -> tuple[str | None, list[str]]:
    """Create a publish tag after a successful put and return any warning."""
    if not result_success or result_dry_run or no_tag or not git_commit:
        return None, []

    try:
        tag_name = expected_tag or build_publish_tag_name(git_commit)
        success, tag_error = create_publish_git_tag(
            git_state.repo_root if git_state is not None else repo_root,
            tag_name,
        )
        if success:
            return tag_name, []
        if tag_error:
            return None, [f"Could not create git tag: {tag_error}"]
    except Exception as exc:
        logger.debug("Git tag creation failed: %s", exc)
        return None, [f"Could not create git tag: {exc}"]

    return None, []


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
