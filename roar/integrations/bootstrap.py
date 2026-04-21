"""Bootstrap helpers for built-in and optional integrations."""

from __future__ import annotations

from .discovery import discover_optional_integrations
from .registry import register_telemetry_provider, register_vcs_provider


def register_builtin_integrations() -> None:
    """Register built-in providers that are part of the core product path."""
    from .git import GitVCSProvider
    from .telemetry import WandBTelemetryProvider

    register_vcs_provider("git", GitVCSProvider)
    register_telemetry_provider("wandb", WandBTelemetryProvider)


def bootstrap_integrations() -> None:
    """Register built-in providers and discover optional providers."""
    register_builtin_integrations()
    discover_optional_integrations()


__all__ = [
    "bootstrap_integrations",
    "register_builtin_integrations",
]
