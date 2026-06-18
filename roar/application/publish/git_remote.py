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

import os
import shutil
import subprocess
from collections.abc import Iterable
from pathlib import Path

# How long the BatchMode auth probe may take before we assume the real push
# would block on interaction. Kept short: the probe only resolves auth, and a
# slow/unreachable host shouldn't stall `roar register`.
_SSH_PROBE_TIMEOUT_SECONDS = 8


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


def list_remote_names(repo_root: Path | str) -> list[str]:
    """Return the names of git remotes configured in this repo (empty if none)."""
    try:
        raw = _git_check_output(repo_root, "remote", capture_stderr=False)
    except subprocess.CalledProcessError:
        return []
    return [line.strip() for line in raw.splitlines() if line.strip()]


# Back-compat alias for internal callers.
_list_remote_names = list_remote_names


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


# ---------------------------------------------------------------------------
# SSH passphrase-prompt prediction
#
# `push_roar_tags` runs `git push` with inherited stdin, so a passphrase-
# protected SSH key that isn't loaded in ssh-agent makes git prompt for the
# passphrase mid-register — with no context for *why* roar is asking for a
# password. `warn_if_ssh_passphrase_prompt_likely` predicts that, cheaply, so
# the caller can print a heads-up first.
#
# Cost-escalating cascade (each step gates the next; every step fails open —
# any uncertainty about tooling/parsing must never block or break the push):
#   1. Is the remote even SSH?            (git remote get-url — local)
#   2. Could the chosen key need a        (ssh -G / ssh-add -l / ssh-keygen —
#      passphrase and not be cached?       local, sub-second)
#   3. Confirm definitively               (BatchMode auth probe — one network
#                                           round-trip, only when 1+2 say maybe)
# ---------------------------------------------------------------------------


def _run_command(
    args: list[str],
    *,
    cwd: Path | str | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str] | None:
    """Run a command capturing text output; return None if it can't run.

    Best-effort helper for the prediction cascade: a missing binary, timeout,
    or OS error yields ``None`` rather than raising, so prediction never breaks
    the push it's trying to annotate.
    """
    if shutil.which(args[0]) is None:
        return None
    try:
        return subprocess.run(
            args,
            cwd=str(cwd) if cwd is not None else None,
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def remote_push_url(repo_root: Path | str, remote: str) -> str | None:
    """Return the push URL for `remote`, or None if it can't be resolved."""
    result = _run_command(["git", "remote", "get-url", "--push", remote], cwd=repo_root)
    if result is None or result.returncode != 0:
        return None
    url = result.stdout.strip()
    return url or None


def ssh_host_from_url(url: str) -> str | None:
    """Extract the `[user@]host` ssh target from a git remote URL.

    Returns None for non-SSH transports (https/git/file/local paths), so the
    caller skips the rest of the cascade. Handles both ``ssh://`` URLs and the
    scp-like ``git@host:path`` shorthand, and preserves ssh config aliases
    (so ``ssh -G`` resolves the user's real IdentityFile).
    """
    candidate = url.strip()
    if not candidate:
        return None

    if candidate.startswith("ssh://"):
        rest = candidate[len("ssh://") :]
        authority = rest.split("/", 1)[0]
        if "@" in authority:
            authority = authority.split("@", 1)[1]
        host = authority.split(":", 1)[0]  # strip :port
        return host or None

    # Any other explicit scheme (https://, git://, file://, http://) is not SSH.
    scheme_sep = candidate.find("://")
    if scheme_sep != -1:
        return None

    # scp-like shorthand: [user@]host:path — a colon before any slash, and no
    # Windows drive-letter false positive (those have a backslash path).
    colon = candidate.find(":")
    if colon == -1:
        return None
    slash = candidate.find("/")
    if slash != -1 and slash < colon:
        return None  # looks like a local path with a colon further in
    authority = candidate[:colon]
    if "@" in authority:
        authority = authority.split("@", 1)[1]
    return authority or None


def _ssh_identity_files(host: str) -> list[Path]:
    """Resolve the candidate IdentityFile paths ssh would consider for `host`."""
    result = _run_command(["ssh", "-G", host], timeout=_SSH_PROBE_TIMEOUT_SECONDS)
    if result is None or result.returncode != 0:
        return []
    files: list[Path] = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped.lower().startswith("identityfile "):
            continue
        raw = stripped.split(None, 1)[1].strip()
        expanded = Path(os.path.expanduser(raw))
        if expanded.exists() and expanded not in files:
            files.append(expanded)
    return files


def _fingerprint_field(line: str) -> str | None:
    """Pull the SHA256:/MD5: fingerprint (2nd field) out of a key listing line."""
    parts = line.split()
    if len(parts) >= 2 and (":" in parts[1]):
        return parts[1]
    return None


def _agent_fingerprints() -> set[str]:
    """Fingerprints of keys currently loaded (decrypted) in ssh-agent."""
    result = _run_command(["ssh-add", "-l"])
    if result is None or result.returncode != 0:
        # rc 1 (no identities) / rc 2 (no agent) / missing binary → none cached.
        return set()
    fps: set[str] = set()
    for line in result.stdout.splitlines():
        fp = _fingerprint_field(line)
        if fp:
            fps.add(fp)
    return fps


def _key_fingerprint(key_path: Path) -> str | None:
    """Fingerprint of a key file (works on encrypted OpenSSH keys — the public
    half is stored unencrypted — so this never needs the passphrase)."""
    result = _run_command(["ssh-keygen", "-lf", str(key_path)])
    if result is None or result.returncode != 0:
        return None
    first = result.stdout.splitlines()[0] if result.stdout.splitlines() else ""
    return _fingerprint_field(first)


def _key_is_encrypted(key_path: Path) -> bool | None:
    """True if the key needs a passphrase, False if not, None if undeterminable.

    Derives the public key with an empty passphrase: success ⇒ unencrypted.
    """
    result = _run_command(["ssh-keygen", "-y", "-P", "", "-f", str(key_path)])
    if result is None:
        return None
    return result.returncode != 0


def _passphrase_prompt_plausible(host: str) -> bool:
    """Cheap local gate: could pushing to `host` block on a key passphrase?

    True when some candidate key is (possibly) encrypted and not already cached
    in the agent, or when ssh keys can't be introspected at all (let the probe
    decide). False only when every candidate key is provably unencrypted or
    already loaded — in which case no passphrase prompt can occur and the
    network probe is skipped entirely.
    """
    keys = _ssh_identity_files(host)
    if not keys:
        return True  # can't introspect — defer to the definitive probe

    agent_fps = _agent_fingerprints()
    for key in keys:
        fingerprint = _key_fingerprint(key)
        cached = fingerprint is not None and fingerprint in agent_fps
        if cached:
            continue
        # Not cached: a passphrase prompt is possible unless we can prove the
        # key is unencrypted. `None` (undeterminable) counts as "possible".
        if _key_is_encrypted(key) is not False:
            return True
    return False


def _batchmode_push_would_block(repo_root: Path | str, remote: str) -> bool:
    """Probe auth with prompts disabled; True if the real push would block/fail.

    `BatchMode=yes` + `GIT_TERMINAL_PROMPT=0` make ssh/git fail instead of
    prompting, so a clean exit proves the real push authenticates silently.
    """
    env = {
        **os.environ,
        "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o ConnectTimeout=5",
        "GIT_TERMINAL_PROMPT": "0",
    }
    result = _run_command(
        ["git", "ls-remote", "--quiet", remote, "HEAD"],
        cwd=repo_root,
        env=env,
        timeout=_SSH_PROBE_TIMEOUT_SECONDS,
    )
    if result is None:
        return False  # couldn't probe (e.g. timeout/no git) — don't cry wolf
    return result.returncode != 0


def warn_if_ssh_passphrase_prompt_likely(repo_root: Path | str, remote: str) -> str | None:
    """Return a heads-up message if the upcoming tag push will likely prompt for
    an SSH key passphrase, else None.

    Runs the cost-escalating cascade and only does the network probe when the
    cheap local checks say a prompt is plausible. Fails open everywhere: any
    uncertainty about the underlying tooling yields None (stay quiet) so this
    never blocks or delays a push it can't reason about.
    """
    url = remote_push_url(repo_root, remote)
    if url is None:
        return None
    host = ssh_host_from_url(url)
    if host is None:
        return None  # not an SSH remote
    if not _passphrase_prompt_plausible(host):
        return None  # keys are unencrypted or already cached in ssh-agent
    if not _batchmode_push_would_block(repo_root, remote):
        return None  # probe authenticated silently — the push won't prompt

    return (
        f"Pushing roar tags to '{remote}' over SSH may prompt for your key passphrase.\n"
        "Tip: run `ssh-add` first to cache it in your ssh-agent and avoid repeated prompts."
    )
