"""Shared dependency assembly for publish workflows."""

from __future__ import annotations

from dataclasses import dataclass

from ...integrations.glaas import GlaasClient
from ...integrations.glaas.registration import (
    ArtifactRegistrationService,
    JobRegistrationService,
    RegistrationCoordinator,
    SessionRegistrationService,
)
from .lineage import LineageCollector


@dataclass(frozen=True)
class PublishRuntime:
    """Concrete dependency set for publish workflows."""

    glaas_client: GlaasClient
    session_service: SessionRegistrationService
    registration_coordinator: RegistrationCoordinator
    lineage_collector: LineageCollector


def build_publish_runtime(
    *,
    glaas_url: str | None = None,
    start_dir: str | None = None,
    allow_public_without_binding: bool = False,
) -> PublishRuntime:
    """Build the default dependency stack for publish entrypoints."""
    glaas_client = GlaasClient(
        glaas_url,
        start_dir=start_dir,
        allow_public_without_binding=allow_public_without_binding,
    )
    session_service = SessionRegistrationService(glaas_client)
    artifact_service = ArtifactRegistrationService(glaas_client)
    job_service = JobRegistrationService(glaas_client)
    return PublishRuntime(
        glaas_client=glaas_client,
        session_service=session_service,
        registration_coordinator=RegistrationCoordinator(
            session_service=session_service,
            artifact_service=artifact_service,
            job_service=job_service,
        ),
        lineage_collector=LineageCollector(),
    )
