"""
Logger implementation for roar internal diagnostics.

Wraps stdlib logging with configurable handlers for console (stderr) and file.
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, ClassVar

from ..core.interfaces.logger import ILogger


class RoarLogger(ILogger):
    """
    Logger implementation using stdlib logging.

    Supports dual output to stderr and ~/.roar/roar.log.
    """

    LOG_FILE_PATH = Path.home() / ".roar" / "roar.log"
    MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
    BACKUP_COUNT = 3

    LEVEL_MAP: ClassVar[dict[str, int]] = {
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "warning": logging.WARNING,
        "error": logging.ERROR,
    }

    def __init__(
        self,
        name: str = "roar",
        level: str = "warning",
        console_enabled: bool = False,
        file_enabled: bool = True,
    ) -> None:
        """
        Initialize logger.

        Args:
            name: Logger name
            level: Initial log level (debug, info, warning, error)
            console_enabled: Enable stderr output
            file_enabled: Enable file output to ~/.roar/roar.log
        """
        self._logger = logging.getLogger(name)
        self._logger.setLevel(logging.DEBUG)  # Let handlers filter
        self._logger.handlers.clear()  # Remove any existing handlers
        self._logger.propagate = False  # Don't propagate to root logger

        self._console_handler: logging.Handler | None = None
        self._file_handler: logging.Handler | None = None

        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        log_level = self.LEVEL_MAP.get(level.lower(), logging.WARNING)

        # Console handler (stderr)
        if console_enabled:
            console = logging.StreamHandler(sys.stderr)
            console.setLevel(log_level)
            console.setFormatter(formatter)
            self._logger.addHandler(console)
            self._console_handler = console

        # File handler with rotation
        if file_enabled:
            self._setup_file_handler(formatter, log_level)

    def _setup_file_handler(self, formatter: logging.Formatter, level: int) -> None:
        """Set up rotating file handler."""
        # Ensure directory exists
        self.LOG_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)

        file_handler = RotatingFileHandler(
            self.LOG_FILE_PATH,
            maxBytes=self.MAX_FILE_SIZE,
            backupCount=self.BACKUP_COUNT,
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        self._logger.addHandler(file_handler)
        self._file_handler = file_handler

    def debug(self, message: str, *args: Any, **kwargs: Any) -> None:
        """Log a debug-level message."""
        self._logger.debug(message, *args, **kwargs)

    def info(self, message: str, *args: Any, **kwargs: Any) -> None:
        """Log an info-level message."""
        self._logger.info(message, *args, **kwargs)

    def warning(self, message: str, *args: Any, **kwargs: Any) -> None:
        """Log a warning-level message."""
        self._logger.warning(message, *args, **kwargs)

    def error(self, message: str, *args: Any, **kwargs: Any) -> None:
        """Log an error-level message."""
        self._logger.error(message, *args, **kwargs)

    def set_level(self, level: str) -> None:
        """Set log level for all handlers."""
        lvl = self.LEVEL_MAP.get(level.lower(), logging.WARNING)
        if self._console_handler:
            self._console_handler.setLevel(lvl)
        if self._file_handler:
            self._file_handler.setLevel(lvl)


class NullLogger(ILogger):
    """No-op logger for testing or when logging is disabled."""

    def debug(self, message: str, *args: Any, **kwargs: Any) -> None:
        """No-op."""
        pass

    def info(self, message: str, *args: Any, **kwargs: Any) -> None:
        """No-op."""
        pass

    def warning(self, message: str, *args: Any, **kwargs: Any) -> None:
        """No-op."""
        pass

    def error(self, message: str, *args: Any, **kwargs: Any) -> None:
        """No-op."""
        pass

    def set_level(self, level: str) -> None:
        """No-op."""
        pass
