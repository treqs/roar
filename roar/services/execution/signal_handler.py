"""
Signal handler service for graceful interrupt handling during command execution.

Replaces global state with encapsulated instance state for better testability
and cleaner design (Single Responsibility Principle).
"""

import os
import signal
import sys
from collections.abc import Callable
from signal import Handlers

from ...core.interfaces.logger import ILogger


class ProcessSignalHandler:
    """
    Manages signal handling for child process execution.

    Encapsulates interrupt state and provides callbacks for interrupt events.
    Follows SRP: only handles signal management.
    """

    def __init__(
        self,
        on_first_interrupt: Callable[[], None] | None = None,
        on_abort: Callable[[], None] | None = None,
        log_files: list[str] | None = None,
        logger: ILogger | None = None,
    ) -> None:
        """
        Initialize signal handler.

        Args:
            on_first_interrupt: Callback when first Ctrl-C is received
            on_abort: Callback when second Ctrl-C is received (abort)
            log_files: List of log file paths to clean up on abort
            logger: Logger for internal diagnostics
        """
        self._interrupted = False
        self._interrupt_count = 0
        self._on_first_interrupt = on_first_interrupt
        self._on_abort = on_abort
        self._log_files = log_files or []
        self._original_handler: Handlers | None = None
        self._logger = logger

    @property
    def logger(self) -> ILogger:
        """Get logger, resolving from container or creating NullLogger."""
        if self._logger is None:
            from ...core.container import get_container
            from ...services.logging import NullLogger

            container = get_container()
            self._logger = container.try_resolve(ILogger)  # type: ignore[type-abstract]
            if self._logger is None:
                self._logger = NullLogger()
        return self._logger

    def install(self) -> None:
        """Install signal handlers."""
        self.logger.debug("Installing SIGINT handler")
        self._original_handler = signal.signal(signal.SIGINT, self._handle_signal)  # type: ignore[assignment]
        self.logger.debug("SIGINT handler installed, original handler saved")

    def restore(self) -> None:
        """Restore original signal handlers."""
        if self._original_handler is not None:
            self.logger.debug("Restoring original SIGINT handler")
            signal.signal(signal.SIGINT, self._original_handler)
            self._original_handler = None

    def is_interrupted(self) -> bool:
        """Check if execution was interrupted."""
        return self._interrupted

    def should_abort(self) -> bool:
        """Check if execution should abort (double Ctrl-C)."""
        return self._interrupt_count >= 2

    def get_interrupt_count(self) -> int:
        """Get number of times interrupted."""
        return self._interrupt_count

    def _handle_signal(self, signum: int, frame) -> None:
        """Handle SIGINT signal."""
        self._interrupt_count += 1
        self._interrupted = True
        self.logger.debug("SIGINT received: interrupt_count=%d", self._interrupt_count)

        if self._interrupt_count == 1:
            self.logger.debug("First interrupt, allowing graceful shutdown")
            if self._on_first_interrupt:
                self._on_first_interrupt()
        elif self._interrupt_count >= 2:
            self.logger.debug("Second interrupt, aborting immediately")
            if self._on_abort:
                self._on_abort()
            # Clean up log files before exit
            self._cleanup_files()
            sys.exit(130)  # Standard exit code for SIGINT

    def _cleanup_files(self) -> None:
        """Clean up log files."""
        self.logger.debug("Cleaning up %d log files", len(self._log_files))
        for log_file in self._log_files:
            try:
                if log_file and os.path.exists(log_file):
                    self.logger.debug("Removing log file: %s", log_file)
                    os.remove(log_file)
            except OSError as e:
                self.logger.debug("Failed to remove log file %s: %s", log_file, e)

    def set_log_files(self, log_files: list[str]) -> None:
        """Set log files to clean up on abort."""
        self.logger.debug("Setting log files for cleanup: %s", log_files)
        self._log_files = log_files
