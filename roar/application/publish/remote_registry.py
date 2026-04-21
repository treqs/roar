"""Remote lineage registry transport contracts for publish workflows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class RemoteRegistryTransport(Protocol):
    """Narrow transport contract for remote lineage registration workflows."""

    name: str
    client: Any
    session_service: Any | None
    registration_coordinator: Any | None

    @property
    def publish_auth(self) -> Any: ...

    def is_configured(self) -> bool: ...

    def health_check(self) -> Any: ...

    def register_composite_artifact(self, payload: dict[str, Any]) -> Any: ...

    def sync_labels(self, payloads: list[dict[str, Any]]) -> Any: ...


@dataclass(frozen=True)
class GlaasRemoteRegistryTransport:
    """GLaaS-backed implementation of the remote registry transport contract."""

    client: Any
    session_service: Any | None = None
    registration_coordinator: Any | None = None
    name: str = "glaas"

    @property
    def publish_auth(self) -> Any:
        return getattr(self.client, "publish_auth", None)

    def is_configured(self) -> bool:
        return bool(self.client.is_configured())

    def health_check(self) -> Any:
        return self.client.health_check()

    def register_composite_artifact(self, payload: dict[str, Any]) -> Any:
        return self.client.register_composite_artifact(payload)

    def sync_labels(self, payloads: list[dict[str, Any]]) -> Any:
        return self.client.sync_labels(payloads)


def coerce_remote_registry(
    *,
    remote_registry: RemoteRegistryTransport | None = None,
    glaas_client: Any | None = None,
    session_service: Any | None = None,
    registration_coordinator: Any | None = None,
) -> RemoteRegistryTransport:
    """Resolve the concrete remote registry transport from new or legacy inputs."""
    if remote_registry is not None:
        return remote_registry
    if glaas_client is None:
        raise ValueError("remote registry transport requires a client")
    return GlaasRemoteRegistryTransport(
        client=glaas_client,
        session_service=session_service,
        registration_coordinator=registration_coordinator,
    )


__all__ = [
    "GlaasRemoteRegistryTransport",
    "RemoteRegistryTransport",
    "coerce_remote_registry",
]
