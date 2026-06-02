"""Request DTOs for `roar get` application flows."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GetRequest:
    """Application request for a get workflow."""

    source: str
    destination: Path
    roar_dir: Path
    cwd: Path
    repo_root: Path | None = None
    message: str | None = None
    expected_hash: str | None = None
    dry_run: bool = False
    force: bool = False
    tag: bool = False
    step_name: str | None = None
    limit: int | None = None  # download only the first N data files (subset get)
