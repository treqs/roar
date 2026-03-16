"""Logging utilities for roar."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from .interfaces.logger import ILogger

_ACTIVE_LOGGER: ILogger | None = None

if TYPE_CHECKING:
    from typing import Any


class RoarLogger(ILogger):
    """Logger implementation using stdlib logging."""

    LOG_FILE_PATH = Path.home() / ".roar" / "roar.log"
    MAX_FILE_SIZE = 10 * 1024 * 1024
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
        self._logger = logging.getLogger(name)
        self._logger.setLevel(logging.DEBUG)
        self._logger.handlers.clear()
        self._logger.propagate = False

        self._console_handler: logging.Handler | None = None
        self._file_handler: logging.Handler | None = None

        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        log_level = self.LEVEL_MAP.get(level.lower(), logging.WARNING)

        if console_enabled:
            console = logging.StreamHandler(sys.stderr)
            console.setLevel(log_level)
            console.setFormatter(formatter)
            self._logger.addHandler(console)
            self._console_handler = console

        if file_enabled:
            self._setup_file_handler(formatter, log_level)

    def _setup_file_handler(self, formatter: logging.Formatter, level: int) -> None:
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
        self._logger.debug(message, *args, **kwargs)

    def info(self, message: str, *args: Any, **kwargs: Any) -> None:
        self._logger.info(message, *args, **kwargs)

    def warning(self, message: str, *args: Any, **kwargs: Any) -> None:
        self._logger.warning(message, *args, **kwargs)

    def error(self, message: str, *args: Any, **kwargs: Any) -> None:
        self._logger.error(message, *args, **kwargs)

    def set_level(self, level: str) -> None:
        log_level = self.LEVEL_MAP.get(level.lower(), logging.WARNING)
        if self._console_handler:
            self._console_handler.setLevel(log_level)
        if self._file_handler:
            self._file_handler.setLevel(log_level)


class NullLogger(ILogger):
    """No-op logger for tests and unbootstrapped code paths."""

    def debug(self, message: str, *args: Any, **kwargs: Any) -> None:
        pass

    def info(self, message: str, *args: Any, **kwargs: Any) -> None:
        pass

    def warning(self, message: str, *args: Any, **kwargs: Any) -> None:
        pass

    def error(self, message: str, *args: Any, **kwargs: Any) -> None:
        pass

    def set_level(self, level: str) -> None:
        pass


def get_logger() -> ILogger:
    """Get the configured logger instance or a NullLogger before bootstrap."""
    return _ACTIVE_LOGGER or NullLogger()  # type: ignore[type-abstract]


def configure_logger(
    *,
    level: str = "warning",
    console_enabled: bool = False,
    file_enabled: bool = True,
) -> ILogger:
    """Configure and cache the process-wide logger."""
    global _ACTIVE_LOGGER
    _ACTIVE_LOGGER = RoarLogger(
        level=level,
        console_enabled=console_enabled,
        file_enabled=file_enabled,
    )
    return _ACTIVE_LOGGER


def reset_logger() -> None:
    """Reset the cached logger for tests and fresh bootstrap."""
    global _ACTIVE_LOGGER
    _ACTIVE_LOGGER = None
