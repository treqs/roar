"""Resolve the canonical git remote and push roar tags during register.

`roar register` writes a record to GLaaS that references one or more
deterministic, roar-namespaced tags (one per distinct commit in the
registered lineage). For those references to resolve outside the
author's machine the tags have to live on a remote — which is the
purpose of this module.

Public surface:

  resolve_canonical_remote(repo_root, configured_remote)
      Pick the remote roar should push to. Hard-error when ambiguous so
      we never push to the wrong place silently.

  push_roar_tags(repo_root, remote, tag_names)
      Push the specific named tags (refspec-explicit; never
      `git push --tags`) and fail-closed if any tag fails.

Both raise `GitRemoteError` for predictable user-facing failures; the
register flow surfaces those as `roar register` errors with the verbatim
git output appended.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterable
from pathlib import Path


class GitRemoteError(RuntimeError):
    """Raised when remote resolution or tag push cannot proceed.

    The message is intended to be shown to the user verbatim — include
    any git stderr that's relevant so they can act on it.
    """


def _git_check_output(repo_root: Path | str, *args: str, capture_stderr: bool) -> str:
    """Run a git subcommand returning stdout. Raises CalledProcessError on failure.

    `capture_stderr=True` merges stderr into stdout so the caller can
    include git's diagnostic output in error messages (e.g. push failures
    where the auth error is on stderr). `False` discards stderr so
    unrelated noise — e.g. `ld.so` warnings inherited from a parent
    process — doesn't pollute the parsed result.
    """
    return subprocess.check_output(
        ["git", *args],
        cwd=str(repo_root),
        text=True,
        stderr=subprocess.STDOUT if capture_stderr else subprocess.DEVNULL,
    )


def _list_remote_names(repo_root: Path | str) -> list[str]:
    try:
        raw = _git_check_output(repo_root, "remote", capture_stderr=False)
    except subprocess.CalledProcessError:
        return []
    return [line.strip() for line in raw.splitlines() if line.strip()]


def resolve_canonical_remote(repo_root: Path | str, configured_remote: str | None) -> str:
    """Return the name of the remote roar should push tags to.

    Resolution order:
      1. `configured_remote` if set (from `git.remote`) — must exist as a
         git remote in this repo.
      2. The single remote returned by `git remote` if exactly one exists.
      3. Hard-error otherwise.
    """
    remotes = _list_remote_names(repo_root)
    if configured_remote:
        if configured_remote not in remotes:
            raise GitRemoteError(
                f"Configured git.remote='{configured_remote}' is not a remote in this repo. "
                f"Existing remotes: {', '.join(remotes) or '(none)'}.\n"
                "Add one with `git remote add <name> <url>`, or unset the override "
                "via `roar config set git.remote ''`."
            )
        return configured_remote

    if not remotes:
        raise GitRemoteError(
            "No git remote configured. `roar register` needs a remote to push "
            "roar tags to so anyone reading the GLaaS record can resolve them "
            "back to the commit.\n"
            "Add one: `git remote add origin <url>`.\n"
            "Or skip the push: `roar config set git.push_tags_on_register never` "
            "(commit links on GLaaS may be broken)."
        )
    if len(remotes) > 1:
        raise GitRemoteError(
            f"Multiple git remotes found ({', '.join(remotes)}). roar can't guess "
            "which one is canonical.\n"
            "Set the canonical remote: "
            "`roar config set git.remote <name>`."
        )
    return remotes[0]


def push_roar_tags(repo_root: Path | str, remote: str, tag_names: Iterable[str]) -> list[str]:
    """Push the named tags to `remote` in a single `git push`.

    Uses fully-qualified refspecs so this never touches tags outside the
    roar namespace, even if the names happen to overlap with something
    else. Returns the list of tags actually requested (after de-dup).
    Raises `GitRemoteError` if the push fails — message includes the
    verbatim git output.
    """
    unique_tags = sorted(set(tag_names))
    if not unique_tags:
        return []
    refspecs = [f"refs/tags/{tag}" for tag in unique_tags]
    try:
        # Capture stderr here so auth/push errors surface in the message.
        _git_check_output(repo_root, "push", remote, *refspecs, capture_stderr=True)
    except subprocess.CalledProcessError as exc:
        raise GitRemoteError(
            f"Failed to push roar tags to '{remote}'.\n"
            f"Tags: {', '.join(unique_tags)}\n"
            f"\n{exc.output or '(no output)'}".rstrip()
        ) from exc
    return unique_tags
