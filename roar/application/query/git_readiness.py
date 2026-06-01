"""Compute and render a one-line git-readiness summary for `roar status`.

`roar status` is the natural place to learn whether the next `roar run`
would be refused, since `roar run` requires a clean working tree. This
module classifies the working tree into one of four states and renders
a single-line summary for the status output:

  - clean        → "clean — branch @ commit  ('roar run' ready)"
  - dirty        → "dirty — 3 modified, 1 untracked  ('roar run' will be refused)"
  - not_a_repo   → "not a git repo  ('roar run' works; not commit-tagged)"
  - home_dir     → "running from $HOME  ('roar run' will be refused)"

The categorization follows `git status --porcelain` v1: the X (index)
and Y (worktree) status codes are mapped onto user-friendly buckets
(modified / untracked / added / deleted / renamed / conflict). Each
porcelain line counts in exactly one bucket; conflict beats everything
since it's the most actionable signal.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

GitReadinessState = Literal["clean", "dirty", "not_a_repo", "home_dir"]


@dataclass(frozen=True)
class GitReadinessSummary:
    state: GitReadinessState
    modified: int = 0
    untracked: int = 0
    added: int = 0
    deleted: int = 0
    renamed: int = 0
    conflict: int = 0
    branch: str | None = None
    short_commit: str | None = None

    @property
    def total_dirty(self) -> int:
        return (
            self.modified
            + self.untracked
            + self.added
            + self.deleted
            + self.renamed
            + self.conflict
        )

    @property
    def run_ready(self) -> bool:
        return self.state == "clean"

    def render_line(self) -> str:
        """One-line summary for `roar status` text output."""
        if self.state == "clean":
            head_parts = []
            if self.branch:
                head_parts.append(self.branch)
            if self.short_commit:
                head_parts.append(f"@ {self.short_commit}")
            head = " ".join(head_parts) if head_parts else ""
            tail = "'roar run' ready"
            return f"clean — {head}  ({tail})" if head else f"clean  ({tail})"
        if self.state == "dirty":
            return f"dirty — {self._dirty_breakdown()}  ('roar run' will be refused)"
        if self.state == "home_dir":
            return "running from $HOME  ('roar run' will be refused)"
        return "not a git repo  ('roar run' works; not commit-tagged)"

    def _dirty_breakdown(self) -> str:
        parts: list[tuple[int, str]] = [
            (self.modified, "modified"),
            (self.untracked, "untracked"),
            (self.added, "added"),
            (self.deleted, "deleted"),
            (self.renamed, "renamed"),
            (self.conflict, "conflict"),
        ]
        return ", ".join(f"{n} {label}" for n, label in parts if n)


def _categorize_porcelain_line(line: str) -> str | None:
    """Map one `git status --porcelain` line to a single bucket.

    Priority (first match wins): conflict > renamed > deleted > added
    > modified > untracked. Returns None for an unparseable line.
    """
    if len(line) < 2:
        return None
    x, y = line[0], line[1]

    # Untracked is its own thing — always exactly "??".
    if x == "?" and y == "?":
        return "untracked"

    # Conflict: any line with U on either side, plus the dual-add/dual-delete cases.
    if "U" in (x, y) or (x, y) in {("A", "A"), ("D", "D")}:
        return "conflict"

    if x == "R" or x == "C":
        return "renamed"
    if x == "D" or y == "D":
        return "deleted"
    if x == "A":
        return "added"
    if x == "M" or y == "M":
        return "modified"
    return None


def _resolve_repo_root(start_dir: Path) -> str | None:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(start_dir),
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None
    return out or None


def _is_home_dir(repo_root: str) -> bool:
    home = os.path.expanduser("~")
    if not home:
        return False
    try:
        return Path(repo_root).resolve() == Path(home).resolve()
    except OSError:
        return False


def _short_commit(repo_root: str) -> str | None:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo_root,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None
    return out or None


def _branch_name(repo_root: str) -> str | None:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo_root,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None
    if not out or out == "HEAD":
        return None
    return out


def _porcelain(repo_root: str) -> str:
    try:
        return subprocess.check_output(
            ["git", "status", "--porcelain"],
            cwd=repo_root,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return ""


def collect_git_readiness(start_dir: Path | str) -> GitReadinessSummary:
    """Compute the readiness summary by inspecting the git repo at start_dir."""
    start = Path(start_dir)
    repo_root = _resolve_repo_root(start)
    if repo_root is None:
        return GitReadinessSummary(state="not_a_repo")
    if _is_home_dir(repo_root):
        # Even if the worktree happens to be clean, running from $HOME is
        # almost certainly a mistake — surface it the same way `roar run`
        # does.
        return GitReadinessSummary(
            state="home_dir",
            branch=_branch_name(repo_root),
            short_commit=_short_commit(repo_root),
        )

    counts: dict[str, int] = {
        "modified": 0,
        "untracked": 0,
        "added": 0,
        "deleted": 0,
        "renamed": 0,
        "conflict": 0,
    }
    for line in _porcelain(repo_root).splitlines():
        bucket = _categorize_porcelain_line(line)
        if bucket is not None:
            counts[bucket] += 1

    branch = _branch_name(repo_root)
    short_commit = _short_commit(repo_root)
    state: GitReadinessState = "dirty" if any(counts.values()) else "clean"
    return GitReadinessSummary(
        state=state,
        branch=branch,
        short_commit=short_commit,
        **counts,
    )
