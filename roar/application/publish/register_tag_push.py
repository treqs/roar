"""Tag + push step inserted before the GLaaS register write.

Today `roar register` writes the GLaaS record first and creates a single
local roar tag afterward (best-effort, no push). The result is a record
that references a tag living only on the author's machine, which is the
bug P1-23 fixes.

This module replaces that with a fail-closed preflight:

  1. Enumerate every distinct git commit in the registered lineage
     (session commit + each job's `git_commit`).
  2. Build the deterministic roar tag name for each.
  3. Create the tags locally (idempotent — the existing `create_roar_git_tag`
     already short-circuits if the tag exists).
  4. Push the tags to the canonical remote, fail-closed.

The caller (publish/service.py) calls `ensure_roar_tags_pushed` between
prepare and the GLaaS write; on failure the GLaaS write is never
attempted, so we never produce a record whose references aren't yet
resolvable.

The returned `RegisterTagSummary` describes what happened so the CLI can
render the matching user-facing lines.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from ...core.interfaces.lineage import LineageData
from ...core.interfaces.logger import ILogger
from ..git import build_roar_git_tag_name, create_roar_git_tag
from .git_remote import (
    GitRemoteError,
    list_remote_names,
    push_roar_tags,
    resolve_canonical_remote,
    warn_if_ssh_passphrase_prompt_likely,
)
from .results import RegisterTagSummary


class TagPushError(RuntimeError):
    """Raised when tagging or pushing fails in a way the user must act on.

    The CLI renders the message verbatim. Wrap underlying git errors so
    the user sees both what we tried and what git said about it.
    """


def _distinct_job_commits(lineage: LineageData | None, session_commit: str) -> list[str]:
    """Distinct job commits in the lineage, excluding the session commit.

    Deterministic order so output is stable across re-registers.
    """
    if lineage is None:
        return []
    seen: set[str] = set()
    ordered: list[str] = []
    for job in lineage.jobs:
        if not isinstance(job, dict):
            continue
        commit = job.get("git_commit")
        if not isinstance(commit, str) or not commit:
            continue
        if commit == session_commit or commit in seen:
            continue
        seen.add(commit)
        ordered.append(commit)
    return sorted(ordered)


def _create_local_tags(repo_root: Path, commit_to_tag: Iterable[tuple[str, str]]) -> None:
    """Create the tags locally, each pointing at its specific commit.

    Idempotent — `create_roar_git_tag` short-circuits if the tag exists.
    Passes the explicit commit SHA so an unreachable commit fails fast
    rather than silently tagging HEAD.
    """
    for commit, tag_name in commit_to_tag:
        success, err = create_roar_git_tag(repo_root, tag_name, target_commit=commit)
        if not success:
            raise TagPushError(
                f"Failed to create roar tag '{tag_name}' for commit {commit}.\n"
                f"{err or '(no error message from git)'}\n"
                "If the commit is no longer reachable locally (e.g. rebase, "
                "garbage collection), recover it via `git reflog` before retrying — "
                "or set `git.push_tags_on_register=never` to register local-only "
                "(commit links on GLaaS may be broken)."
            )


def ensure_roar_tags_pushed(
    *,
    repo_root: Path,
    session_commit: str,
    session_tag_name: str | None,
    lineage: LineageData | None,
    dry_run: bool,
    tagging_enabled: bool,
    configured_remote: str | None,
    push_mode: str,
    logger: ILogger,
) -> RegisterTagSummary:
    """Create + push roar tags for the lineage commits.

    `session_tag_name` is computed by the caller (today: from current HEAD,
    via `build_roar_git_tag_name`). Job-commit tags are derived here so
    the session-tag logic stays untouched.

    Returns a `RegisterTagSummary` describing what happened. Raises
    `TagPushError` if the push fails — the caller must NOT proceed to the
    GLaaS write in that case.
    """
    if dry_run:
        return RegisterTagSummary(push_skipped_reason="dry_run")
    if not tagging_enabled:
        return RegisterTagSummary(push_skipped_reason="tagging_disabled")

    job_commits = _distinct_job_commits(lineage, session_commit)
    job_tags: tuple[str, ...] = tuple(build_roar_git_tag_name(c, short=True) for c in job_commits)

    # Build the commit→tag list for the actual create-tag calls. Session
    # is first so its tag is created/pushed even if there are no jobs.
    pairs: list[tuple[str, str]] = []
    if session_tag_name:
        pairs.append((session_commit, session_tag_name))
    pairs.extend(zip(job_commits, job_tags, strict=True))

    if not pairs:
        # Nothing to tag — tagging is off in some other way (e.g. no
        # commit). Match the legacy no-op behavior.
        return RegisterTagSummary(push_skipped_reason="no_commits")

    _create_local_tags(repo_root, pairs)

    if push_mode == "never":
        logger.debug(
            "Tags created but not pushed (git.push_tags_on_register=never): %s",
            [tag for _, tag in pairs],
        )
        return RegisterTagSummary(
            session_tag=session_tag_name,
            job_tags=job_tags,
            push_skipped_reason="never_config",
        )

    # No remote at all (and none configured): degrade to a local-only register
    # instead of blocking. The common "does this work" loop shouldn't be gated on
    # having a remote — and the dry-run never blocks, so the real run shouldn't
    # contradict it. Tags exist locally; the reproducibility checklist surfaces
    # that the commit isn't reachable on a remote. (An *invalid* configured
    # remote or ambiguous multi-remote setup still errors below — those mean the
    # user wants a push but roar can't safely pick where.)
    if not configured_remote and not list_remote_names(repo_root):
        logger.debug("No git remote configured — registering local-only (tags not pushed).")
        return RegisterTagSummary(
            session_tag=session_tag_name,
            job_tags=job_tags,
            push_skipped_reason="no_remote",
        )

    try:
        remote = resolve_canonical_remote(repo_root, configured_remote)
    except GitRemoteError as exc:
        raise TagPushError(str(exc)) from exc

    # The push below inherits stdin, so a passphrase-protected SSH key not in
    # ssh-agent makes git prompt mid-register with no context. Predict that
    # (best-effort) and tell the user *why* a password is about to be asked for.
    passphrase_hint = warn_if_ssh_passphrase_prompt_likely(repo_root, remote)
    if passphrase_hint:
        logger.warning(passphrase_hint)

    try:
        push_roar_tags(repo_root, remote, [tag for _, tag in pairs])
    except GitRemoteError as exc:
        raise TagPushError(str(exc)) from exc

    return RegisterTagSummary(
        session_tag=session_tag_name,
        job_tags=job_tags,
        remote=remote,
    )
