from __future__ import annotations

from unittest.mock import MagicMock, patch

from roar.application.publish.lineage import LineageCollector
from roar.application.publish.runtime import build_publish_runtime


def test_build_publish_runtime_builds_shared_dependency_stack() -> None:
    client = MagicMock()
    session_service = MagicMock()
    artifact_service = MagicMock()
    job_service = MagicMock()
    coordinator = MagicMock()

    with (
        patch("roar.application.publish.runtime.GlaasClient", return_value=client) as client_cls,
        patch(
            "roar.application.publish.runtime.SessionRegistrationService",
            return_value=session_service,
        ) as session_cls,
        patch(
            "roar.application.publish.runtime.ArtifactRegistrationService",
            return_value=artifact_service,
        ) as artifact_cls,
        patch(
            "roar.application.publish.runtime.JobRegistrationService",
            return_value=job_service,
        ) as job_cls,
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
    client_cls.assert_called_once_with(
        "http://localhost:3001",
        start_dir=None,
        allow_public_without_binding=False,
    )
    session_cls.assert_called_once_with(client)
    artifact_cls.assert_called_once_with(client)
    job_cls.assert_called_once_with(client)
    coordinator_cls.assert_called_once_with(
        session_service=session_service,
        artifact_service=artifact_service,
        job_service=job_service,
    )
