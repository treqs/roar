"""
Logger interface for internal diagnostic output.

Separate from IPresenter which handles user-facing output.
Use ILogger for debug/diagnostic messages that should be configurable
and potentially written to log files.
"""

from abc import ABC, abstractmethod
from typing import Any


class ILogger(ABC):
    """
    Interface for internal logging.

    Used for debug/diagnostic output - NOT for user-facing messages.
    User-facing output should use IPresenter.
    """

    @abstractmethod
    def debug(self, message: str, *args: Any, **kwargs: Any) -> None:
        """Log a debug-level message."""
        pass

    @abstractmethod
    def info(self, message: str, *args: Any, **kwargs: Any) -> None:
        """Log an info-level message."""
        pass

    @abstractmethod
    def warning(self, message: str, *args: Any, **kwargs: Any) -> None:
        """Log a warning-level message."""
        pass

    @abstractmethod
    def error(self, message: str, *args: Any, **kwargs: Any) -> None:
        """Log an error-level message."""
        pass

    @abstractmethod
    def set_level(self, level: str) -> None:
        """
        Set the logging level.

        Args:
            level: One of 'debug', 'info', 'warning', 'error'
        """
        pass
