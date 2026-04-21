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
from .remote_registry import GlaasRemoteRegistryTransport, RemoteRegistryTransport


@dataclass(frozen=True)
class PublishRuntime:
    """Concrete dependency set for publish workflows."""

    remote_registry: RemoteRegistryTransport
    lineage_collector: LineageCollector

    @property
    def glaas_client(self):
        """Backward-compatible access to the underlying GLaaS client."""
        return self.remote_registry.client

    @property
    def session_service(self):
        """Backward-compatible access to the publish session service."""
        return self.remote_registry.session_service

    @property
    def registration_coordinator(self):
        """Backward-compatible access to the registration coordinator."""
        return self.remote_registry.registration_coordinator


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
    registration_coordinator = RegistrationCoordinator(
        session_service=session_service,
        artifact_service=artifact_service,
        job_service=job_service,
    )
    return PublishRuntime(
        remote_registry=GlaasRemoteRegistryTransport(
            client=glaas_client,
            session_service=session_service,
            registration_coordinator=registration_coordinator,
        ),
        lineage_collector=LineageCollector(),
    )
