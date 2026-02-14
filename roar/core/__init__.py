"""
Core infrastructure for roar's dependency injection and plugin architecture.

This module provides:
- ServiceContainer: DI container
- Plugin registry with auto-discovery
- Application bootstrap for initialization
- Protocol definitions for all service interfaces
- Custom exception hierarchy

All public names are lazily imported on first access to keep
``import roar.core.settings`` (and similar lightweight imports) fast.
"""

__all__ = [
    "CloudDownloadError",
    "CloudUploadError",
    "ConfigFileError",
    "ConfigValidationError",
    "DatabaseConnectionError",
    "GlaasAPIError",
    "GlaasConnectionError",
    "PluginLoadError",
    "RoarCloudError",
    "RoarConfigError",
    "RoarDatabaseError",
    "RoarException",
    "RoarExecutionError",
    "RoarNetworkError",
    "RoarPluginError",
    "RoarValidationError",
    "ServiceContainer",
    "TracerNotFoundError",
    "bootstrap",
    "discover_commands",
    "discover_plugins",
    "get_container",
    "is_initialized",
    "reset",
    "resolve",
    "try_resolve",
]

# Map public names to (module, name) for lazy resolution.
_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    "bootstrap": (".bootstrap", "bootstrap"),
    "is_initialized": (".bootstrap", "is_initialized"),
    "reset": (".bootstrap", "reset"),
    "ServiceContainer": (".container", "ServiceContainer"),
    "get_container": (".container", "get_container"),
    "resolve": (".container", "resolve"),
    "try_resolve": (".container", "try_resolve"),
    "discover_commands": (".registry", "discover_commands"),
    "discover_plugins": (".registry", "discover_plugins"),
    # exceptions
    "CloudDownloadError": (".exceptions", "CloudDownloadError"),
    "CloudUploadError": (".exceptions", "CloudUploadError"),
    "ConfigFileError": (".exceptions", "ConfigFileError"),
    "ConfigValidationError": (".exceptions", "ConfigValidationError"),
    "DatabaseConnectionError": (".exceptions", "DatabaseConnectionError"),
    "GlaasAPIError": (".exceptions", "GlaasAPIError"),
    "GlaasConnectionError": (".exceptions", "GlaasConnectionError"),
    "PluginLoadError": (".exceptions", "PluginLoadError"),
    "RoarCloudError": (".exceptions", "RoarCloudError"),
    "RoarConfigError": (".exceptions", "RoarConfigError"),
    "RoarDatabaseError": (".exceptions", "RoarDatabaseError"),
    "RoarException": (".exceptions", "RoarException"),
    "RoarExecutionError": (".exceptions", "RoarExecutionError"),
    "RoarNetworkError": (".exceptions", "RoarNetworkError"),
    "RoarPluginError": (".exceptions", "RoarPluginError"),
    "RoarValidationError": (".exceptions", "RoarValidationError"),
    "TracerNotFoundError": (".exceptions", "TracerNotFoundError"),
}


def __getattr__(name: str):  # noqa: N807 — PEP 562 module __getattr__
    if name in _LAZY_IMPORTS:
        module_path, attr = _LAZY_IMPORTS[name]
        import importlib

        mod = importlib.import_module(module_path, __name__)
        value = getattr(mod, attr)
        # Cache on module dict so __getattr__ isn't called again.
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
