"""Core public API for bootstrap and exceptions."""

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
    "TracerNotFoundError",
    "bootstrap",
    "is_initialized",
    "reset",
]

# Map public names to (module, name) for lazy resolution.
_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    "bootstrap": (".bootstrap", "bootstrap"),
    "is_initialized": (".bootstrap", "is_initialized"),
    "reset": (".bootstrap", "reset"),
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


def __getattr__(name: str):
    if name in _LAZY_IMPORTS:
        module_path, attr = _LAZY_IMPORTS[name]
        import importlib

        mod = importlib.import_module(module_path, __name__)
        value = getattr(mod, attr)
        # Cache on module dict so __getattr__ isn't called again.
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
