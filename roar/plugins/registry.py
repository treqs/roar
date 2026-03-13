"""Plugin registry with auto-discovery for optional plugins."""

from __future__ import annotations

from ..core.container import get_container
from ..core.interfaces.telemetry import ITelemetryProvider
from ..core.interfaces.vcs import IVCSProvider
from ..core.logging import get_logger as _get_logger


def discover_plugins() -> None:
    """Auto-discover and register optional entrypoint plugins."""
    container = get_container()
    _discover_entrypoint_plugins(container)


def _implements(cls: type, interface: type) -> bool:
    """Return True when a class concretely implements an interface."""
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
    """Discover plugins registered via the ``roar.plugins`` entrypoint group."""
    try:
        from importlib.metadata import entry_points

        eps = entry_points(group="roar.plugins")

        for ep in eps:
            try:
                plugin_cls = ep.load()
                _register_entrypoint_plugin(container, plugin_cls)
            except Exception as exc:
                _get_logger().debug("Failed to load entry point plugin %s: %s", ep.name, exc)
                continue
    except Exception as exc:
        _get_logger().debug("Failed to discover entry point plugins: %s", exc)


def _register_entrypoint_plugin(container, plugin_cls: type) -> None:
    """Register an entry point plugin based on its interface."""
    if _implements(plugin_cls, ITelemetryProvider):
        instance = plugin_cls()
        container.register_telemetry_provider(instance.name, plugin_cls)
    elif _implements(plugin_cls, IVCSProvider):
        instance = plugin_cls()
        container.register_vcs_provider(instance.name, plugin_cls)
