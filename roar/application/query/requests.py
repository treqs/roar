"""Request DTOs for local query and label workflows."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class DagQueryRequest:
    roar_dir: Path
    expanded: bool
    output_json: bool
    use_color: bool
    show_artifacts: bool
    stale_only: bool


ShowQuerySelector = Literal["auto", "session", "path", "job", "artifact"]


@dataclass(frozen=True)
class ShowQueryRequest:
    roar_dir: Path
    cwd: Path
    ref: str | None
    selector: ShowQuerySelector = "auto"


@dataclass(frozen=True)
class StatusQueryRequest:
    roar_dir: Path


@dataclass(frozen=True)
class LineageQueryRequest:
    roar_dir: Path
    cwd: Path
    artifact: str
    output: str
    depth: int


@dataclass(frozen=True)
class LogQueryRequest:
    roar_dir: Path
    use_color: bool


@dataclass(frozen=True)
class LabelSetRequest:
    roar_dir: Path
    cwd: Path
    entity_type: str
    target: str
    pairs: tuple[str, ...]


@dataclass(frozen=True)
class LabelCopyRequest:
    roar_dir: Path
    cwd: Path
    source_entity_type: str
    source_target: str
    destination_entity_type: str
    destination_target: str


@dataclass(frozen=True)
class LabelShowRequest:
    roar_dir: Path
    cwd: Path
    entity_type: str
    target: str


@dataclass(frozen=True)
class LabelHistoryRequest:
    roar_dir: Path
    cwd: Path
    entity_type: str
    target: str


InputsQuerySelector = Literal["auto", "path", "job", "artifact"]


@dataclass(frozen=True)
class InputsQueryRequest:
    roar_dir: Path
    cwd: Path
    ref: str
    selector: InputsQuerySelector = "auto"
    direct: bool = False
    show_all: bool = False
    output_json: bool = False
