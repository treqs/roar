"""Optional integration discovery via Python entry points."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..core.interfaces.telemetry import ITelemetryProvider
from ..core.interfaces.vcs import IVCSProvider
from ..core.logging import get_logger as _get_logger
from .registry import register_telemetry_provider, register_vcs_provider

_LEGACY_ENTRYPOINT_GROUP = "roar.integrations"
_TYPED_PROVIDER_GROUPS: tuple[tuple[str, type, Callable[[str, type], None]], ...] = (
    ("roar.telemetry_providers", ITelemetryProvider, register_telemetry_provider),
    ("roar.vcs_providers", IVCSProvider, register_vcs_provider),
)


def discover_optional_integrations() -> None:
    """Auto-discover and register optional integration providers."""
    _discover_typed_entrypoint_integrations()
    _discover_legacy_entrypoint_integrations()


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


def _discover_typed_entrypoint_integrations() -> None:
    """Discover providers via typed entry-point groups."""
    for group, interface, register in _TYPED_PROVIDER_GROUPS:
        try:
            for entry_point in _iter_entry_points(group):
                try:
                    provider_cls = entry_point.load()
                    _register_typed_entrypoint_provider(provider_cls, interface, register)
                except Exception as exc:
                    _get_logger().debug(
                        "Failed to load integration entry point %s from %s: %s",
                        entry_point.name,
                        group,
                        exc,
                    )
                    continue
        except Exception as exc:
            _get_logger().debug("Failed to discover optional integrations from %s: %s", group, exc)


def _discover_legacy_entrypoint_integrations() -> None:
    """Discover providers registered via the legacy ``roar.integrations`` group."""
    try:
        for entry_point in _iter_entry_points(_LEGACY_ENTRYPOINT_GROUP):
            try:
                provider_cls = entry_point.load()
                _register_entrypoint_provider(provider_cls)
            except Exception as exc:
                _get_logger().debug(
                    "Failed to load integration entry point %s: %s", entry_point.name, exc
                )
                continue
    except Exception as exc:
        _get_logger().debug("Failed to discover optional integrations: %s", exc)


def _register_entrypoint_provider(provider_cls: type) -> None:
    """Register a legacy entry point provider based on its interface."""
    if _implements(provider_cls, ITelemetryProvider):
        instance = provider_cls()
        register_telemetry_provider(instance.name, provider_cls)
    elif _implements(provider_cls, IVCSProvider):
        instance = provider_cls()
        register_vcs_provider(instance.name, provider_cls)


def _register_typed_entrypoint_provider(
    provider_cls: type,
    interface: type,
    register: Callable[[str, type], None],
) -> None:
    """Register an entry point provider from a typed group."""
    if not _implements(provider_cls, interface):
        return
    instance = provider_cls()
    register(instance.name, provider_cls)


def _iter_entry_points(group: str) -> tuple[Any, ...]:
    from importlib.metadata import entry_points

    try:
        return tuple(entry_points(group=group))
    except TypeError:
        all_entry_points = entry_points()
        select = getattr(all_entry_points, "select", None)
        if callable(select):
            return tuple(select(group=group))
        return tuple(all_entry_points.get(group, ()))
