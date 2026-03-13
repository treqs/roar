"""
Plugin registry with auto-discovery for optional plugins.

Automatically discovers and registers plugins from:
1. Entry point plugins from external packages
"""

from .container import get_container
from .interfaces.telemetry import ITelemetryProvider
from .interfaces.vcs import IVCSProvider
from .logging import get_logger as _get_logger


def discover_plugins() -> None:
    """
    Auto-discover and register plugins.

    Discovers optional plugin entry points from external packages.
    """
    container = get_container()

    # Discover entry point plugins (for external packages)
    _discover_entrypoint_plugins(container)


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
        my_provider = "my_package.provider:MyTelemetryProvider"
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
    if _implements(plugin_cls, ITelemetryProvider):
        instance = plugin_cls()
        container.register_telemetry_provider(instance.name, plugin_cls)
    elif _implements(plugin_cls, IVCSProvider):
        instance = plugin_cls()
        container.register_vcs_provider(instance.name, plugin_cls)
