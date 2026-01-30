"""
Plugin registry with auto-discovery.

Automatically discovers and registers plugins from:
1. Built-in plugins in roar.plugins.*
2. Entry point plugins from external packages
"""

import contextlib
import importlib
import pkgutil

from .container import get_container
from .di import resolve_or_default
from .interfaces.cloud import ICloudStorageProvider
from .interfaces.command import ICommand
from .interfaces.logger import ILogger
from .interfaces.telemetry import ITelemetryProvider
from .interfaces.vcs import IVCSProvider


def _get_logger() -> ILogger:
    from ..services.logging import NullLogger

    return resolve_or_default(ILogger, NullLogger)  # type: ignore[type-abstract]


def discover_plugins(package_name: str = "roar.plugins") -> None:
    """
    Auto-discover and register plugins.

    Scans roar.plugins.* for classes implementing provider interfaces.
    Also supports entry points for external plugins.

    Args:
        package_name: Base package to scan for plugins
    """
    container = get_container()

    # Discover built-in plugins
    _discover_builtin_plugins(container, package_name)

    # Discover entry point plugins (for external packages)
    _discover_entrypoint_plugins(container)


def _discover_builtin_plugins(container, package_name: str) -> None:
    """Discover plugins from the built-in plugins package."""
    try:
        importlib.import_module(package_name)
    except ImportError:
        # Plugins package not yet created
        return

    # Walk through plugin subpackages
    subpackages = ["cloud", "telemetry", "vcs", "analyzers"]
    for subpackage in subpackages:
        try:
            subpkg = importlib.import_module(f"{package_name}.{subpackage}")
            _scan_package_for_plugins(container, subpkg, subpackage)
        except ImportError:
            continue


def _scan_package_for_plugins(container, package, plugin_type: str) -> None:
    """Scan a package for plugin classes and register them."""
    package_path = getattr(package, "__path__", None)
    if not package_path:
        return

    for _importer, modname, _ispkg in pkgutil.iter_modules(package_path):
        # Skip private modules and base classes
        if modname.startswith("_") or modname == "base":
            continue

        try:
            module = importlib.import_module(f"{package.__name__}.{modname}")
        except ImportError:
            continue

        # Find and register plugin classes
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if not isinstance(attr, type):
                continue

            _try_register_plugin(container, attr, plugin_type)


def _try_register_plugin(container, cls: type, plugin_type: str) -> None:
    """Try to register a class as a plugin if it implements the right interface."""

    if plugin_type == "cloud" and _implements(cls, ICloudStorageProvider):
        try:
            instance = cls()
            container.register_cloud_provider(instance.scheme, cls)
        except Exception as e:
            _get_logger().debug(
                "Failed to load cloud plugin %s.%s: %s",
                cls.__module__,
                cls.__name__,
                e,
            )

    elif plugin_type == "telemetry" and _implements(cls, ITelemetryProvider):
        try:
            instance = cls()
            container.register_telemetry_provider(instance.name, cls)
        except Exception as e:
            _get_logger().debug(
                "Failed to load telemetry plugin %s.%s: %s",
                cls.__module__,
                cls.__name__,
                e,
            )

    elif plugin_type == "vcs" and _implements(cls, IVCSProvider):
        try:
            instance = cls()
            container.register_vcs_provider(instance.name, cls)
        except Exception as e:
            _get_logger().debug(
                "Failed to load VCS plugin %s.%s: %s",
                cls.__module__,
                cls.__name__,
                e,
            )


def _implements(cls: type, interface: type) -> bool:
    """
    Check if a class implements an interface.

    Returns True if cls is a concrete subclass of interface
    (not the interface itself and not abstract).
    """
    try:
        return (
            isinstance(cls, type)
            and issubclass(cls, interface)
            and cls is not interface
            and not getattr(cls, "__abstractmethods__", set())
        )
    except TypeError:
        return False


def _discover_entrypoint_plugins(container) -> None:
    """
    Discover plugins registered via entry points.

    External packages can register plugins by adding to pyproject.toml:

        [project.entry-points."roar.plugins"]
        my_provider = "my_package.provider:MyCloudProvider"
    """
    try:
        from importlib.metadata import entry_points

        eps = entry_points(group="roar.plugins")

        for ep in eps:
            try:
                plugin_cls = ep.load()
                _register_entrypoint_plugin(container, plugin_cls)
            except Exception as e:
                # Don't fail startup due to broken external plugins
                _get_logger().debug("Failed to load entry point plugin %s: %s", ep.name, e)
                continue
    except Exception as e:
        # importlib.metadata might not be available in very old Python
        _get_logger().debug("Failed to discover entry point plugins: %s", e)


def _register_entrypoint_plugin(container, plugin_cls: type) -> None:
    """Register an entry point plugin based on its interface."""
    if _implements(plugin_cls, ICloudStorageProvider):
        instance = plugin_cls()
        container.register_cloud_provider(instance.scheme, plugin_cls)
    elif _implements(plugin_cls, ITelemetryProvider):
        instance = plugin_cls()
        container.register_telemetry_provider(instance.name, plugin_cls)
    elif _implements(plugin_cls, IVCSProvider):
        instance = plugin_cls()
        container.register_vcs_provider(instance.name, plugin_cls)


# -------------------------------------------------------------------------
# Command discovery
# -------------------------------------------------------------------------


def discover_commands(package_name: str = "roar.commands") -> None:
    """
    Auto-discover and register commands from the commands package.

    Commands are registered by scanning roar.commands.* for classes
    that implement ICommand.

    Args:
        package_name: Base package to scan for commands
    """
    container = get_container()

    try:
        package = importlib.import_module(package_name)
    except ImportError:
        return

    package_path = getattr(package, "__path__", None)
    if not package_path:
        return

    for _importer, modname, _ispkg in pkgutil.iter_modules(package_path):
        # Skip private modules
        if modname.startswith("_"):
            continue

        try:
            module = importlib.import_module(f"{package_name}.{modname}")
        except ImportError:
            continue

        # Find command classes
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if not isinstance(attr, type):
                continue

            if _implements(attr, ICommand):
                with contextlib.suppress(Exception):
                    container.register_command(attr)


# -------------------------------------------------------------------------
# Decorator for manual registration
# -------------------------------------------------------------------------


def register_plugin(cls: type) -> type:
    """
    Decorator to manually register a plugin class.

    Usage:
        @register_plugin
        class MyCloudProvider(ICloudStorageProvider):
            ...

    The decorator auto-detects the interface and registers appropriately.
    """
    container = get_container()

    if _implements(cls, ICloudStorageProvider):
        instance = cls()
        container.register_cloud_provider(instance.scheme, cls)
    elif _implements(cls, ITelemetryProvider):
        instance = cls()
        container.register_telemetry_provider(instance.name, cls)
    elif _implements(cls, IVCSProvider):
        instance = cls()
        container.register_vcs_provider(instance.name, cls)
    elif _implements(cls, ICommand):
        container.register_command(cls)

    return cls
