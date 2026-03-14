"""
Service protocol definitions for run/build command execution.

These protocols define the contracts for services that handle
command execution with provenance tracking.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from roar.core.models.run import ResolvedStep, RunResult

__all__ = [
    "IDAGResolver",
    "IRunReportPresenter",
    "ISignalHandler",
]


@runtime_checkable
class ISignalHandler(Protocol):
    """Protocol for signal handling during command execution."""

    def install(self) -> None:
        """Install signal handlers."""
        ...

    def restore(self) -> None:
        """Restore original signal handlers."""
        ...

    def is_interrupted(self) -> bool:
        """Check if execution was interrupted."""
        ...

    def should_abort(self) -> bool:
        """Check if execution should abort (double Ctrl-C)."""
        ...

    def get_interrupt_count(self) -> int:
        """Get number of times interrupted."""
        ...

    def set_log_files(self, log_files: list[str]) -> None:
        """Set log files to clean up on abort."""
        ...


@runtime_checkable
class IDAGResolver(Protocol):
    """Protocol for resolving DAG step references."""

    def resolve(
        self,
        reference: str,
        param_overrides: dict[str, str],
    ) -> tuple[ResolvedStep | None, str | None]:
        """
        Resolve @N or @BN reference to command.

        Returns (resolved_step, error_message).
        """
        ...


@runtime_checkable
class IRunReportPresenter(Protocol):
    """Protocol for run result presentation."""

    def show_report(
        self,
        result: RunResult,
        command: list[str],
        quiet: bool = False,
    ) -> None:
        """Display run completion report."""
        ...

    def show_stale_warnings(
        self,
        stale_upstream: list[int],
        stale_downstream: list[int],
        is_build: bool = False,
    ) -> None:
        """Display stale step warnings."""
        ...
