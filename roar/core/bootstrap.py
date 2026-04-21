"""Application bootstrap for roar."""

from pathlib import Path

from ..integrations import bootstrap_integrations, reset_integrations
from .logging import configure_logger, reset_logger

_initialized = False


def bootstrap(roar_dir: Path | None = None) -> None:
    """
    Bootstrap the roar application.

    Initializes core process-wide runtime state:
    - Configured logging
    - Built-in integrations (git, telemetry)
    - Optional integrations discovered from entry points

    Args:
        roar_dir: Optional path to .roar directory (reserved for future bootstrap needs)

    """
    global _initialized

    if _initialized:
        return

    _configure_core_logging(roar_dir)

    bootstrap_integrations()

    _initialized = True
    return


def _configure_core_logging(roar_dir: Path | None = None) -> None:
    """Configure the process-wide logger from local config."""
    from ..integrations.config import load_settings

    start_dir: str | None = None
    if roar_dir is not None:
        search_root = roar_dir.parent if roar_dir.name == ".roar" else roar_dir
        start_dir = str(search_root)

    settings = load_settings(start_dir=start_dir)
    level = settings.logging.level or "warning"
    console_enabled = bool(settings.logging.console)
    file_enabled = settings.logging.file
    if file_enabled is None:
        file_enabled = True
    configure_logger(
        level=level,
        console_enabled=console_enabled,
        file_enabled=file_enabled,
    )


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
