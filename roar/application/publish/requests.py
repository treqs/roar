"""Request and result DTOs for publish workflows."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ...services.put.service import PutResult
    from ...services.registration.register_service import RegisterResult
else:
    PutResult = Any
    RegisterResult = Any


@dataclass(frozen=True)
class RegisterLineageRequest:
    """Application request for `roar register`."""

    target: str
    roar_dir: Path
    cwd: Path
    dry_run: bool = False
    as_blake3: bool = False
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
    no_tag: bool = False


@dataclass(frozen=True)
class RegisterLineageResponse:
    """Application response for `roar register`."""

    result: RegisterResult


@dataclass(frozen=True)
class PutResponse:
    """Application response for `roar put`."""

    result: PutResult
    git_tag: str | None = None
    warnings: list[str] = field(default_factory=list)
