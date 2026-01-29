"""
Application bootstrap for roar.

Initializes the DI container with all services and plugins.
This module should be called once at application startup.
"""

from pathlib import Path

from .container import ServiceContainer, get_container
from .interfaces.logger import ILogger
from .interfaces.presenter import IPresenter
from .registry import discover_plugins

_initialized = False


def bootstrap(roar_dir: Path | None = None) -> ServiceContainer:
    """
    Bootstrap the roar application.

    Initializes the DI container with:
    - Core services (database, hashing, etc.)
    - Plugins (cloud providers, telemetry, VCS)

    Args:
        roar_dir: Optional path to .roar directory

    Returns:
        Initialized ServiceContainer
    """
    global _initialized

    container = get_container()

    if _initialized:
        return container

    # Register core services
    _register_core_services(container, roar_dir)

    # Discover and register plugins
    discover_plugins()

    _initialized = True
    return container


def _register_core_services(container: ServiceContainer, roar_dir: Path | None) -> None:
    """Register core application services."""
    from ..config import config_get
    from ..presenters.console import ConsolePresenter
    from ..services.logging import RoarLogger

    # Register default presenter
    container.register_singleton(IPresenter, implementation=ConsolePresenter())  # type: ignore[type-abstract]

    # Register logger with configuration
    def create_logger() -> ILogger:
        level = config_get("logging.level") or "warning"
        console_enabled = config_get("logging.console") or False
        file_enabled = config_get("logging.file")
        if file_enabled is None:
            file_enabled = True
        return RoarLogger(
            level=level,
            console_enabled=console_enabled,
            file_enabled=file_enabled,
        )

    container.register_singleton(ILogger, factory=create_logger)  # type: ignore[type-abstract]

    # Register database services if roar_dir is provided
    if roar_dir and roar_dir.exists():
        _register_database_services(container, roar_dir)


def _register_database_services(container: ServiceContainer, roar_dir: Path) -> None:
    """Register database-related services."""

    # Database services are now accessed via DatabaseContext
    # which is created on-demand using create_database_context(roar_dir)
    # Each command/caller creates its own context as needed
    # No global singleton registration needed since contexts are lightweight


def reset() -> None:
    """
    Reset the application state.

    Useful for testing to ensure clean state between tests.
    """
    global _initialized
    ServiceContainer.reset()
    _initialized = False


def is_initialized() -> bool:
    """Check if the application has been bootstrapped."""
    return _initialized
