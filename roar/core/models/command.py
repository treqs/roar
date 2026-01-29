"""
Command interface models.

Provides Pydantic models for CLI command execution context and results.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import Field, field_validator

from .base import ImmutableModel


class CommandContext(ImmutableModel):
    """Immutable context passed to all commands.

    Contains all information a command needs about the execution environment.
    """

    roar_dir: Path
    repo_root: Path | None = None
    cwd: Path
    args: list[str] = Field(default_factory=list)
    is_interactive: bool = True

    @field_validator("roar_dir", "cwd", mode="before")
    @classmethod
    def ensure_path(cls, v: Any) -> Path:
        """Ensure path fields are Path objects."""
        return Path(v) if not isinstance(v, Path) else v

    @field_validator("repo_root", mode="before")
    @classmethod
    def ensure_optional_path(cls, v: Any) -> Path | None:
        """Ensure optional path field is Path or None."""
        if v is None:
            return None
        return Path(v) if not isinstance(v, Path) else v


class CommandResult(ImmutableModel):
    """Result from command execution.

    Standardizes command return values for consistent handling.
    """

    success: bool
    exit_code: int = 0
    message: str | None = None
    data: dict[str, Any] | None = None

    @classmethod
    def ok(
        cls,
        message: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> CommandResult:
        """Create a successful result.

        Args:
            message: Optional success message
            data: Optional result data

        Returns:
            CommandResult with success=True and exit_code=0
        """
        return cls(success=True, exit_code=0, message=message, data=data)

    @classmethod
    def error(
        cls,
        message: str,
        exit_code: int = 1,
        data: dict[str, Any] | None = None,
    ) -> CommandResult:
        """Create an error result.

        Args:
            message: Error message
            exit_code: Exit code (default 1)
            data: Optional error data

        Returns:
            CommandResult with success=False
        """
        return cls(success=False, exit_code=exit_code, message=message, data=data)
