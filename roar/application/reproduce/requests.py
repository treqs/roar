"""Request DTOs for artifact reproduction workflows."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ReproduceRequest:
    hash_prefix: str
    roar_dir: Path
    cwd: Path
    run_pipeline: bool = False
    auto_confirm: bool = False
    dpkg_any_version: bool = False
    pip_any_version: bool = False
    package_sync: bool = False
    list_requirements: bool = False
    out_path: str | None = None
