"""Shared dependency assembly for publish workflows."""

from __future__ import annotations

from dataclasses import dataclass

from ...glaas_client import GlaasClient
from ...services.registration import RegistrationCoordinator, SessionRegistrationService
from .lineage import LineageCollector


@dataclass(frozen=True)
class PublishRuntime:
    """Concrete dependency set for publish workflows."""

    glaas_client: GlaasClient
    session_service: SessionRegistrationService
    registration_coordinator: RegistrationCoordinator
    lineage_collector: LineageCollector


def build_publish_runtime(*, glaas_url: str | None = None) -> PublishRuntime:
    """Build the default dependency stack for publish entrypoints."""
    glaas_client = GlaasClient(glaas_url)
    return PublishRuntime(
        glaas_client=glaas_client,
        session_service=SessionRegistrationService(glaas_client),
        registration_coordinator=RegistrationCoordinator(),
        lineage_collector=LineageCollector(),
    )
