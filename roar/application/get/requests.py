"""Request/response DTOs for `roar get` application flows."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ...services.get.service import GetResult


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


@dataclass(frozen=True)
class GetResponse:
    """Application response for a get workflow."""

    result: GetResult
    git_tag: str | None = None
    warnings: list[str] = field(default_factory=list)

