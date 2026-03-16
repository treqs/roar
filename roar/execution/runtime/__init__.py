"""Shared runtime entrypoints and host-execution mechanics."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .backup import PreviousOutputBackupService
    from .coordinator import RunCoordinator
    from .host_execution import ExecutionSetupError, execute_host_run
    from .signal_handler import ProcessSignalHandler
    from .tracer import TracerService

__all__ = [
    "ExecutionSetupError",
    "PreviousOutputBackupService",
    "ProcessSignalHandler",
    "RunCoordinator",
    "TracerService",
    "execute_host_run",
]


def __getattr__(name: str):
    """Lazy-load runtime exports so bootstrap imports stay dependency-light."""
    if name == "PreviousOutputBackupService":
        from .backup import PreviousOutputBackupService

        return PreviousOutputBackupService
    if name == "RunCoordinator":
        from .coordinator import RunCoordinator

        return RunCoordinator
    if name in {"ExecutionSetupError", "execute_host_run"}:
        from .host_execution import ExecutionSetupError, execute_host_run

        return {
            "ExecutionSetupError": ExecutionSetupError,
            "execute_host_run": execute_host_run,
        }[name]
    if name == "ProcessSignalHandler":
        from .signal_handler import ProcessSignalHandler

        return ProcessSignalHandler
    if name == "TracerService":
        from .tracer import TracerService

        return TracerService
    raise AttributeError(name)
