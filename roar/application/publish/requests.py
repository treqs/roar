"""Request DTOs for publish workflows."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RegisterLineageRequest:
    """Application request for `roar register`."""

    # None means the whole active session. Keep that intent explicit instead
    # of freezing a pre-bootstrap canonical hash in the CLI.
    target: str | None
    roar_dir: Path
    cwd: Path
    dry_run: bool = False
    as_blake3: bool = False
    public: bool = False
    anonymous: bool = False
    skip_confirmation: bool = False
    confirm_callback: Callable[[list[str]], bool] | None = None


@dataclass(frozen=True)
class PutRequest:
    """Application request for `roar put`."""

    roar_dir: Path
    cwd: Path
    repo_root: Path | None
    sources: list[str]
    destination: str
    message: str
    dry_run: bool = False
    public: bool = False
    anonymous: bool = False
    no_tag: bool = False
    step_name: str | None = None
    as_dataset: bool = False  # declare the source dir(s) a dataset even if unstructured
