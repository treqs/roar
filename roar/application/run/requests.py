"""Request DTOs for tracked run/build workflows."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class RunRequest:
    roar_dir: Path
    cwd: Path
    args: tuple[str, ...] = field(default_factory=tuple)
    quiet: bool | None = None
    step_name: str | None = None
    tracer_mode: str | None = None
    tracer_fallback: bool | None = None
    hash_algorithms: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class BuildRequest:
    roar_dir: Path
    cwd: Path
    args: tuple[str, ...] = field(default_factory=tuple)
    quiet: bool | None = None
    step_name: str | None = None
    tracer_mode: str | None = None
    tracer_fallback: bool | None = None
    hash_algorithms: tuple[str, ...] = field(default_factory=tuple)
