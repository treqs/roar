"""
Dependency injection helpers for roar.

Provides utilities for working with the service container, including
lazy resolution patterns that allow fallback to default implementations.

This module extracts duplicated DI resolution patterns from various
command and service files.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable

T = TypeVar("T")


def resolve_or_default(
    interface: type[T],
    default_factory: Callable[[], T],
) -> T:
    """Resolve a service from the container or create a default.

    This helper implements the common pattern of trying to resolve
    a service from the DI container, but falling back to a default
    implementation if the container isn't configured or the service
    isn't registered.

    Args:
        interface: The interface/protocol type to resolve
        default_factory: Callable that creates the default implementation

    Returns:
        Resolved service instance or default

    Example:
        >>> from roar.core.interfaces.logger import ILogger
        >>> from roar.services.logging import NullLogger
        >>> logger = resolve_or_default(ILogger, NullLogger)
    """
    try:
        from .container import get_container

        container = get_container()
        instance = container.try_resolve(interface)
        if instance is not None:
            return instance
    except Exception:
        # Container not bootstrapped or resolution failed
        pass

    return default_factory()


def try_resolve(interface: type[T]) -> T | None:
    """Try to resolve a service from the container.

    Returns None if the container isn't available or the service
    isn't registered, rather than raising an exception.

    Args:
        interface: The interface/protocol type to resolve

    Returns:
        Resolved service instance or None

    Example:
        >>> from roar.core.interfaces.logger import ILogger
        >>> logger = try_resolve(ILogger)
        >>> if logger:
        ...     logger.info("Logging available")
    """
    try:
        from .container import get_container

        container = get_container()
        return container.try_resolve(interface)
    except Exception:
        return None


def is_bootstrapped() -> bool:
    """Check if the service container has been bootstrapped.

    Returns:
        True if the container is available and bootstrapped

    Example:
        >>> if is_bootstrapped():
        ...     container = get_container()
        ...     # Use container services
    """
    try:
        from .container import get_container

        container = get_container()
        return container is not None
    except Exception:
        return False


def require_bootstrap() -> None:
    """Ensure the container is bootstrapped.

    Raises:
        RuntimeError: If the container hasn't been bootstrapped

    Example:
        >>> require_bootstrap()  # Raises if not bootstrapped
        >>> container = get_container()  # Safe to use
    """
    if not is_bootstrapped():
        raise RuntimeError(
            "Service container not bootstrapped. Call bootstrap() before using container services."
        )


class LazyService:
    """Descriptor for lazy service resolution.

    Use this as a class attribute to defer service resolution until
    first access. This is useful for avoiding import cycles and
    ensuring services are only resolved when needed.

    Example:
        class MyCommand:
            _logger = LazyService(ILogger, NullLogger)

            @property
            def logger(self) -> ILogger:
                return self._logger
    """

    def __init__(
        self,
        interface: type[T],
        default_factory: Callable[[], T],
    ) -> None:
        self.interface = interface
        self.default_factory = default_factory
        self._instance: T | None = None
        self._resolved = False

    def __get__(self, obj: object, objtype: type | None = None) -> T:  # type: ignore[type-var]
        if not self._resolved:
            self._instance = resolve_or_default(
                self.interface,
                self.default_factory,
            )
            self._resolved = True
        return self._instance  # type: ignore[return-value]
