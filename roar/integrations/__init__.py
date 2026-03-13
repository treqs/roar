"""Integration adapters and provider registries for external systems."""

from .registry import (
    get_all_telemetry_providers,
    get_integration_registry,
    get_telemetry_provider,
    get_vcs_provider,
    list_telemetry_providers,
    list_vcs_providers,
    register_telemetry_provider,
    register_vcs_provider,
    reset_integrations,
)

__all__ = [
    "get_all_telemetry_providers",
    "get_integration_registry",
    "get_telemetry_provider",
    "get_vcs_provider",
    "list_telemetry_providers",
    "list_vcs_providers",
    "register_telemetry_provider",
    "register_vcs_provider",
    "reset_integrations",
]
