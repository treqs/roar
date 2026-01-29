"""
Service protocol definitions for run/build command execution.

These protocols define the contracts for services that handle
command execution with provenance tracking.
"""

from pathlib import Path
from typing import Protocol, runtime_checkable

# Re-export models for backward compatibility
from roar.core.models.run import (
    ResolvedStep,
    RunArguments,
    RunContext,
    RunResult,
    TracerResult,
)


@runtime_checkable
class IRunArgumentParser(Protocol):
    """Protocol for parsing run command arguments."""

    def parse(self, args: list[str], job_type: str | None = None) -> RunArguments:
        """Parse command-line arguments."""
        ...

    def get_help_text(self, is_build: bool = False) -> str:
        """Get help text for the command."""
        ...


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

    def set_log_files(self, log_files: "list[str]") -> None:
        """Set log files to clean up on abort."""
        ...


@runtime_checkable
class ITracerService(Protocol):
    """Protocol for process tracing."""

    def find_tracer(self) -> str | None:
        """Find the tracer binary."""
        ...

    def execute(
        self,
        command: list[str],
        roar_dir: Path,
        signal_handler: "ISignalHandler",
    ) -> TracerResult:
        """Execute command with tracing."""
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
class IRunCoordinator(Protocol):
    """Protocol for coordinating run execution."""

    def execute(self, ctx: RunContext) -> RunResult:
        """Execute a complete run with all tracking."""
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
