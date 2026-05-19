"""Classify dirty-tree paths into buckets so the start-of-run error
can recommend the right fix per bucket.

Three buckets:

* **code** — tracked file with an uncommitted modification (porcelain
  status code starts with ``M`` / ``A`` / ``D`` / ``R`` / ``C`` / ``U``).
  Right fix is ``git add`` + ``git commit``.
* **roar_output** — untracked path that the local roar DB already
  knows about (artifact ``first_seen_path`` matches). Looks like an
  output of an earlier ``roar run``. Right fix is ``.gitignore`` —
  or commit if the user really wants the file in the repo.
* **unknown** — untracked, no match in the local DB. Could be a stray
  log, a fresh dataset, anything. Offer both options.

We deliberately do *not* hash untracked files to look for content
matches in the DB. Hashing every untracked file on every blocked run
isn't worth it just to choose between two near-identical hint blocks
(``roar_output`` vs ``unknown`` both recommend the same fix — the only
difference is the framing line).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class DirtyClassification:
    """Bucketed view of a ``git status --porcelain`` output."""

    code: list[str] = field(default_factory=list)
    roar_outputs: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.code or self.roar_outputs or self.unknown)

    @property
    def all_code(self) -> bool:
        return bool(self.code) and not self.roar_outputs and not self.unknown

    @property
    def all_roar_outputs(self) -> bool:
        return bool(self.roar_outputs) and not self.code and not self.unknown

    @property
    def all_unknown(self) -> bool:
        return bool(self.unknown) and not self.code and not self.roar_outputs


class _ArtifactLookup(Protocol):
    """Minimal protocol the classifier needs from the DB."""

    def get_by_path(self, path: str) -> Any: ...


def classify_dirty_paths(
    status_output: str,
    repo_root: str | Path,
    artifact_lookup: _ArtifactLookup | None,
) -> DirtyClassification:
    """Bucket each path in a porcelain output into code / roar-output / unknown.

    ``artifact_lookup`` may be ``None`` when no DB context is available
    (very early in a fresh init, for example). In that case the
    classifier never produces ``roar_output`` entries — everything
    untracked falls through to ``unknown``.
    """
    code: list[str] = []
    roar_outputs: list[str] = []
    unknown: list[str] = []

    repo_path = Path(repo_root)
    for status_code, path in _parse_porcelain(status_output):
        if _is_tracked_modification(status_code):
            code.append(path)
            continue
        if artifact_lookup is not None and _is_known_roar_output(artifact_lookup, repo_path, path):
            roar_outputs.append(path)
            continue
        unknown.append(path)

    return DirtyClassification(code=code, roar_outputs=roar_outputs, unknown=unknown)


def _parse_porcelain(status_output: str) -> list[tuple[str, str]]:
    """Yield ``(status_code, path)`` pairs from porcelain v1 output.

    Porcelain v1 format is ``XY path``, where ``X`` and ``Y`` are the
    index/worktree status codes. For renames the line is ``XY old -> new``;
    we keep the new path (that's what the user works with).
    """
    pairs: list[tuple[str, str]] = []
    for line in status_output.splitlines():
        if not line:
            continue
        code = line[:2]
        path = line[3:] if len(line) > 3 else line
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        path = path.strip().strip('"')
        if path:
            pairs.append((code, path))
    return pairs


def _is_tracked_modification(status_code: str) -> bool:
    """A status code other than ``??`` means git is tracking the path."""
    return status_code.strip() != "??"


def _is_known_roar_output(artifact_lookup: _ArtifactLookup, repo_root: Path, rel_path: str) -> bool:
    """True if the path matches an artifact's recorded ``first_seen_path``."""
    abs_path = str((repo_root / rel_path).resolve())
    try:
        record = artifact_lookup.get_by_path(abs_path)
    except Exception:
        return False
    return bool(record)
