from __future__ import annotations

from unittest.mock import MagicMock, patch

from roar.application.publish.lineage import LineageCollector
from roar.application.publish.runtime import build_publish_runtime


def test_build_publish_runtime_builds_shared_dependency_stack() -> None:
    client = MagicMock()
    session_service = MagicMock()
    coordinator = MagicMock()

    with (
        patch("roar.application.publish.runtime.GlaasClient", return_value=client) as client_cls,
        patch(
            "roar.application.publish.runtime.SessionRegistrationService",
            return_value=session_service,
        ) as session_cls,
        patch(
            "roar.application.publish.runtime.RegistrationCoordinator",
            return_value=coordinator,
        ) as coordinator_cls,
    ):
        runtime = build_publish_runtime(glaas_url="http://localhost:3001")

    assert runtime.glaas_client is client
    assert runtime.session_service is session_service
    assert runtime.registration_coordinator is coordinator
    assert isinstance(runtime.lineage_collector, LineageCollector)
    client_cls.assert_called_once_with("http://localhost:3001")
    session_cls.assert_called_once_with(client)
    coordinator_cls.assert_called_once_with()
