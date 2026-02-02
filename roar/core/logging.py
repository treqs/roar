"""Logging utilities for roar.

Provides a centralized way to get logger instances, reducing
boilerplate across the codebase.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .interfaces.logger import ILogger


def get_logger() -> "ILogger":
    """Get the configured logger instance.

    Resolves from DI container if available, otherwise returns NullLogger.
    This is the single entry point for logger resolution across the codebase.

    Usage:
        from roar.core.logging import get_logger

        logger = get_logger()
        logger.debug("Processing %s", item)

    Returns:
        ILogger instance
    """
    from ..services.logging import NullLogger

    from .di import resolve_or_default
    from .interfaces.logger import ILogger

    return resolve_or_default(ILogger, NullLogger)  # type: ignore[type-abstract]
