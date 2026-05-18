"""Post-``roar run`` warning when the working tree is now dirty.

A successful ``roar run`` can leave the repo in a state that the *next*
``roar run`` will refuse — typically because the run produced untracked
outputs that aren't in ``.gitignore``. Telling the user now (one
``warning:`` line + ``hint:`` follow-up) is cheaper than letting them
hit the dirty-tree error on the next invocation and figure it out then.

The check runs ``git status --porcelain`` after the run completes,
filters to untracked entries (tracked-modified is a different kind of
problem the user already knows about), and emits a self-contained
warning line + gitignore suggestions through the same hint primitives
``next_steps_hint`` uses (so quiet mode / ``hints.enabled = false``
suppress consistently).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import IO

from ...presenters.terminal import TerminalCaps, style


def emit_dirty_outputs_warning(
    *,
    repo_root: str | Path,
    stream: IO[str],
    caps: TerminalCaps,
    quiet: bool,
) -> None:
    """Run ``git status`` and warn if untracked outputs will block the next run.

    Silent when the tree is already clean (the happy path), when
    ``quiet`` is set, or when ``hints.enabled = false`` in config.
    """
    if quiet:
        return
    if not _hints_enabled():
        return

    untracked = _untracked_paths(repo_root)
    if not untracked:
        return

    can_color = caps.can_color
    count = len(untracked)
    noun = "output" if count == 1 else "outputs"
    verb = "makes" if count == 1 else "make"

    warning = (
        f"warning: {count} {noun} {verb} this repo dirty and will block the next "
        f"`roar run`. Add to .gitignore or commit."
    )
    # Warnings get yellow (distinct from amber `hint:` lines below) so a
    # reader can tell the actionable-vs-advisory split at a glance.
    _emit(stream, style(warning, "warn_yellow", enabled=can_color))

    from .gitignore_suggest import gitignore_lines

    suggestions = gitignore_lines(untracked)
    for suggestion in suggestions:
        _emit(stream, style(f"hint:     {suggestion}", "warn_amber", enabled=can_color))
    _emit(
        stream,
        style(
            "hint:     git add .gitignore && git commit -m 'ignore roar outputs'",
            "warn_amber",
            enabled=can_color,
        ),
    )


def _hints_enabled() -> bool:
    try:
        from ...integrations.config import config_get

        return config_get("hints.enabled") is not False
    except Exception:
        # If config can't be read, default to showing — the post-run
        # window is short and a missed warning is worse than a stray line.
        return True


def _untracked_paths(repo_root: str | Path) -> list[str]:
    """Return paths git reports as untracked (`??`) relative to repo root.

    Tracked-modified paths (``M``/``A``/``D``/``R``) aren't included —
    those are code changes the user is in the middle of, not artifacts
    the run produced. The next run's dirty-tree error handles them as a
    code-change case.
    """
    try:
        output = subprocess.check_output(
            ["git", "status", "--porcelain"],
            stderr=subprocess.DEVNULL,
            text=True,
            cwd=str(repo_root),
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []

    untracked: list[str] = []
    for line in output.splitlines():
        if not line:
            continue
        code = line[:2]
        if code.strip() != "??":
            continue
        path = line[3:].strip().strip('"')
        if path:
            untracked.append(path)
    return untracked


def _emit(stream: IO[str], line: str) -> None:
    stream.write(line + "\n")
    stream.flush()
