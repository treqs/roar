"""Request DTOs for artifact reproduction workflows."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class ReproduceRequest:
    hash_prefix: str
    roar_dir: Path
    cwd: Path
    target_kind: Literal["artifact", "lineage"] = "artifact"
    run_pipeline: bool = False
    auto_confirm: bool = False
    dpkg_any_version: bool = False
    pip_any_version: bool = False
    package_sync: bool = False
    list_requirements: bool = False
    out_path: str | None = None
    # Write the recorded pip pins to a requirements.txt (for debugging a failed
    # install) instead of previewing/running; None means don't export.
    export_requirements: str | None = None
    # Per-step wall-clock timeout in seconds for --run; None means no timeout.
    step_timeout: int | None = None
    # Skip publish (`roar put`) steps — for third-party reproduction that
    # rebuilds the artifact without re-publishing to the owner's destination.
    no_puts: bool = False
    # Emit an editable reproduction shell script instead of previewing/running.
    emit_script: bool = False
