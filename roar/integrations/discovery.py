"""Optional integration discovery via Python entry points."""

from __future__ import annotations

from ..core.interfaces.telemetry import ITelemetryProvider
from ..core.interfaces.vcs import IVCSProvider
from ..core.logging import get_logger as _get_logger
from .registry import register_telemetry_provider, register_vcs_provider


def discover_optional_integrations() -> None:
    """Auto-discover and register optional integration providers."""
    _discover_entrypoint_integrations()


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


def _discover_entrypoint_integrations() -> None:
    """Discover providers registered via the ``roar.integrations`` entrypoint group."""
    try:
        from importlib.metadata import entry_points

        eps = entry_points(group="roar.integrations")

        for ep in eps:
            try:
                provider_cls = ep.load()
                _register_entrypoint_provider(provider_cls)
            except Exception as exc:
                _get_logger().debug("Failed to load integration entry point %s: %s", ep.name, exc)
                continue
    except Exception as exc:
        _get_logger().debug("Failed to discover optional integrations: %s", exc)


def _register_entrypoint_provider(provider_cls: type) -> None:
    """Register an entry point provider based on its interface."""
    if _implements(provider_cls, ITelemetryProvider):
        instance = provider_cls()
        register_telemetry_provider(instance.name, provider_cls)
    elif _implements(provider_cls, IVCSProvider):
        instance = provider_cls()
        register_vcs_provider(instance.name, provider_cls)
