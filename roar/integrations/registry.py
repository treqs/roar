"""Registry for optional and built-in integration providers."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..core.interfaces.telemetry import ITelemetryProvider
    from ..core.interfaces.vcs import IVCSProvider


class IntegrationRegistry:
    """Global registry for integration provider classes."""

    _instance: IntegrationRegistry | None = None

    def __init__(self) -> None:
        self._telemetry_providers: dict[str, type[ITelemetryProvider]] = {}
        self._vcs_providers: dict[str, type[IVCSProvider]] = {}

    @classmethod
    def get_instance(cls) -> IntegrationRegistry:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        cls._instance = None

    def register_telemetry_provider(
        self,
        name: str,
        provider_class: type[ITelemetryProvider],
    ) -> None:
        self._telemetry_providers[name] = provider_class

    def get_telemetry_provider(self, name: str) -> ITelemetryProvider:
        if name not in self._telemetry_providers:
            raise KeyError(f"No telemetry provider registered: {name}")
        return self._telemetry_providers[name]()

    def get_all_telemetry_providers(self) -> dict[str, ITelemetryProvider]:
        return {name: cls() for name, cls in self._telemetry_providers.items()}

    def list_telemetry_providers(self) -> list[str]:
        return list(self._telemetry_providers.keys())

    def register_vcs_provider(
        self,
        name: str,
        provider_class: type[IVCSProvider],
    ) -> None:
        self._vcs_providers[name] = provider_class

    def get_vcs_provider(self, name: str = "git") -> IVCSProvider:
        if name not in self._vcs_providers:
            raise KeyError(f"No VCS provider registered: {name}")
        return self._vcs_providers[name]()

    def list_vcs_providers(self) -> list[str]:
        return list(self._vcs_providers.keys())


def get_integration_registry() -> IntegrationRegistry:
    """Return the global integration registry."""
    return IntegrationRegistry.get_instance()


def reset_integrations() -> None:
    """Reset the global integration registry."""
    IntegrationRegistry.reset()


def register_telemetry_provider(
    name: str,
    provider_class: type[ITelemetryProvider],
) -> None:
    get_integration_registry().register_telemetry_provider(name, provider_class)


def get_telemetry_provider(name: str) -> ITelemetryProvider:
    return get_integration_registry().get_telemetry_provider(name)


def get_all_telemetry_providers() -> dict[str, ITelemetryProvider]:
    return get_integration_registry().get_all_telemetry_providers()


def list_telemetry_providers() -> list[str]:
    return get_integration_registry().list_telemetry_providers()


def register_vcs_provider(name: str, provider_class: type[IVCSProvider]) -> None:
    get_integration_registry().register_vcs_provider(name, provider_class)


def get_vcs_provider(name: str = "git") -> IVCSProvider:
    return get_integration_registry().get_vcs_provider(name)


def list_vcs_providers() -> list[str]:
    return get_integration_registry().list_vcs_providers()
