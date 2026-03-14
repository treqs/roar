"""Application bootstrap for roar."""

from pathlib import Path

from ..integrations import register_telemetry_provider, register_vcs_provider, reset_integrations
from ..plugins import discover_plugins
from .logging import configure_logger, reset_logger

_initialized = False


def bootstrap(roar_dir: Path | None = None) -> None:
    """
    Bootstrap the roar application.

    Initializes the DI container with:
    - Core services (database, hashing, etc.)
    - Built-in integrations (git, telemetry)
    - Optional plugins discovered from the plugin registry

    Args:
        roar_dir: Optional path to .roar directory

    """
    global _initialized

    if _initialized:
        return

    _configure_core_logging()

    # Register built-in integrations that should not depend on plugin discovery.
    _register_builtin_integrations()

    # Discover and register plugins
    discover_plugins()

    _initialized = True
    return


def _configure_core_logging() -> None:
    """Configure the process-wide logger from local config."""
    from ..integrations.config import config_get

    level = config_get("logging.level") or "warning"
    console_enabled = config_get("logging.console") or False
    file_enabled = config_get("logging.file")
    if file_enabled is None:
        file_enabled = True
    configure_logger(
        level=level,
        console_enabled=console_enabled,
        file_enabled=file_enabled,
    )


def _register_builtin_integrations() -> None:
    """Register built-in integrations that are part of the core product path."""
    from ..integrations.git import GitVCSProvider
    from ..integrations.telemetry import WandBTelemetryProvider

    register_vcs_provider("git", GitVCSProvider)
    register_telemetry_provider("wandb", WandBTelemetryProvider)


def reset() -> None:
    """
    Reset the application state.

    Useful for testing to ensure clean state between tests.
    """
    global _initialized
    reset_logger()
    reset_integrations()
    _initialized = False


def is_initialized() -> bool:
    """Check if the application has been bootstrapped."""
    return _initialized
