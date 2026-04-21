from __future__ import annotations

from unittest.mock import MagicMock

from roar.application.publish.remote_registry import (
    GlaasRemoteRegistryTransport,
    coerce_remote_registry,
)


def test_glaas_remote_registry_transport_delegates_to_client() -> None:
    client = MagicMock()
    client.is_configured.return_value = True
    client.health_check.return_value = None
    client.register_composite_artifact.return_value = {"ok": True}
    client.sync_labels.return_value = ({"ok": True}, None)
    client.publish_auth = {"mode": "device"}
    session_service = MagicMock()
    coordinator = MagicMock()

    transport = GlaasRemoteRegistryTransport(
        client=client,
        session_service=session_service,
        registration_coordinator=coordinator,
    )

    assert transport.publish_auth == {"mode": "device"}
    assert transport.is_configured() is True
    assert transport.health_check() is None
    assert transport.register_composite_artifact({"hash": "abc"}) == {"ok": True}
    assert transport.sync_labels([{"entity_type": "artifact"}]) == ({"ok": True}, None)
    assert transport.session_service is session_service
    assert transport.registration_coordinator is coordinator


def test_coerce_remote_registry_returns_existing_transport() -> None:
    transport = GlaasRemoteRegistryTransport(client=MagicMock())

    assert coerce_remote_registry(remote_registry=transport) is transport
