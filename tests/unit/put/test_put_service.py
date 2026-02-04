"""
Unit tests for the put service orchestrator.

Tests the end-to-end put workflow: resolve → upload → create job.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

from roar.core.interfaces.registration import BatchRegistrationResult, SessionRegistrationResult
from roar.core.interfaces.upload import LineageData
from roar.services.put.backends import MemoryBackend
from roar.services.put.service import PutService


def create_mock_glaas_deps():
    """Create mock GLaaS dependencies for testing."""
    mock_client = MagicMock()
    mock_client.health_check.return_value = True

    mock_session_service = MagicMock()
    mock_session_service.compute_session_hash.return_value = "session_hash_abc123"
    mock_session_service.register.return_value = SessionRegistrationResult(
        success=True,
        session_hash="session_hash_abc123",
        session_url="https://glaas.ai/dag/session_hash_abc123",
    )

    mock_lineage_collector = MagicMock()
    mock_lineage_collector.collect.return_value = LineageData(
        jobs=[],
        artifacts=[],
        artifact_hashes=set(),
        pipeline={"id": 1},
    )

    mock_coordinator = MagicMock()
    mock_coordinator.register_lineage.return_value = BatchRegistrationResult(
        session_registered=True,
        jobs_created=0,
        jobs_failed=0,
        artifacts_registered=0,
        artifacts_failed=0,
        links_created=0,
        links_failed=0,
        errors=[],
    )

    return {
        "glaas_client": mock_client,
        "session_service": mock_session_service,
        "lineage_collector": mock_lineage_collector,
        "registration_coordinator": mock_coordinator,
    }


class TestPutServiceBasic:
    """Tests for basic put service functionality."""

    def test_put_single_file_creates_job(self, tmp_path: Path):
        """Put a single file creates a job record with the file as input."""
        # Arrange
        model_file = tmp_path / "model.pt"
        model_file.write_bytes(b"model data")

        backend = MemoryBackend(bucket="test-bucket", prefix="models")

        mock_db = Mock()
        mock_db.artifacts = Mock()
        mock_db.artifacts.register.return_value = ("artifact-uuid-1", True)
        mock_db.jobs = Mock()
        mock_db.jobs.create.return_value = (42, "job-uid-1")
        mock_db.sessions = Mock()
        mock_db.sessions.get_active.return_value = {"id": 1, "current_step": 2}
        mock_db.sessions.get_next_step_number.return_value = 3

        glaas_deps = create_mock_glaas_deps()

        with patch("roar.services.put.service.get_glaas_url", return_value="http://glaas.test"):
            service = PutService(
                db_context=mock_db,
                backend=backend,
                destination="memory://test-bucket/models",
                repo_root=tmp_path,
                **glaas_deps,
            )

            # Act
            result = service.put(
                sources=[str(model_file)],
                message="publish model",
            )

            # Assert
            assert result.success is True
            assert result.job_id == 42
            assert len(result.uploaded_files) == 1
            assert result.uploaded_files[0]["local_path"] == str(model_file)
            assert "memory://test-bucket/models/model.pt" in result.uploaded_files[0]["remote_url"]

            # Verify job was created
            mock_db.jobs.create.assert_called_once()
            call_kwargs = mock_db.jobs.create.call_args[1]
            assert "roar put" in call_kwargs["command"]
            assert call_kwargs["session_id"] == 1
            assert call_kwargs["step_number"] == 3

            # Verify artifact was linked as input
            mock_db.jobs.add_input.assert_called_once()

    def test_put_stores_urls_in_job_metadata(self, tmp_path: Path):
        """Put stores cloud URLs in job metadata."""
        # Arrange
        model_file = tmp_path / "model.pt"
        model_file.write_bytes(b"model data")

        backend = MemoryBackend(bucket="bucket", prefix="run-1")

        mock_db = Mock()
        mock_db.artifacts = Mock()
        mock_db.artifacts.register.return_value = ("artifact-uuid-1", True)
        mock_db.jobs = Mock()
        mock_db.jobs.create.return_value = (42, "job-uid")
        mock_db.sessions = Mock()
        mock_db.sessions.get_active.return_value = {"id": 1}
        mock_db.sessions.get_next_step_number.return_value = 1

        glaas_deps = create_mock_glaas_deps()

        with patch("roar.services.put.service.get_glaas_url", return_value="http://glaas.test"):
            service = PutService(
                db_context=mock_db,
                backend=backend,
                destination="memory://test-bucket/models",
                repo_root=tmp_path,
                **glaas_deps,
            )

            # Act
            service.put(sources=[str(model_file)], message="test")

            # Assert
            call_kwargs = mock_db.jobs.create.call_args[1]
            metadata = json.loads(call_kwargs["metadata"])
            assert "put" in metadata
            assert metadata["put"]["message"] == "test"
            assert "artifacts" in metadata["put"]
            # Should have at least one artifact URL
            assert len(metadata["put"]["artifacts"]) == 1

    def test_put_multiple_files(self, tmp_path: Path):
        """Put multiple files uploads all and links as inputs."""
        # Arrange
        file1 = tmp_path / "model.pt"
        file2 = tmp_path / "config.yaml"
        file1.write_bytes(b"model")
        file2.write_bytes(b"config")

        backend = MemoryBackend(bucket="bucket", prefix="")

        mock_db = Mock()
        mock_db.artifacts = Mock()
        artifact_ids = iter([("art-1", True), ("art-2", True)])
        mock_db.artifacts.register.side_effect = lambda **kwargs: next(artifact_ids)
        mock_db.jobs = Mock()
        mock_db.jobs.create.return_value = (42, "job-uid")
        mock_db.sessions = Mock()
        mock_db.sessions.get_active.return_value = {"id": 1}
        mock_db.sessions.get_next_step_number.return_value = 1

        glaas_deps = create_mock_glaas_deps()

        with patch("roar.services.put.service.get_glaas_url", return_value="http://glaas.test"):
            service = PutService(
                db_context=mock_db,
                backend=backend,
                destination="memory://test-bucket/models",
                repo_root=tmp_path,
                **glaas_deps,
            )

            # Act
            result = service.put(sources=[str(file1), str(file2)], message="test")

            # Assert
            assert result.success is True
            assert len(result.uploaded_files) == 2
            assert mock_db.jobs.add_input.call_count == 2


class TestPutServiceDryRun:
    """Tests for dry run mode."""

    def test_dry_run_does_not_upload(self, tmp_path: Path):
        """Dry run doesn't upload files or create job."""
        # Arrange
        model_file = tmp_path / "model.pt"
        model_file.write_bytes(b"model data")

        backend = MemoryBackend(bucket="bucket", prefix="")
        mock_db = Mock()
        mock_db.sessions = Mock()
        mock_db.sessions.get_active.return_value = {"id": 1}

        glaas_deps = create_mock_glaas_deps()

        with patch("roar.services.put.service.get_glaas_url", return_value="http://glaas.test"):
            service = PutService(
                db_context=mock_db,
                backend=backend,
                destination="memory://test-bucket/models",
                repo_root=tmp_path,
                **glaas_deps,
            )

            # Act
            result = service.put(
                sources=[str(model_file)],
                message="test",
                dry_run=True,
            )

            # Assert
            assert result.success is True
            assert result.dry_run is True
            assert result.job_id is None
            assert len(result.would_upload) == 1
            assert backend.exists("model.pt") is False
            mock_db.jobs.create.assert_not_called()


class TestPutServiceErrors:
    """Tests for error handling."""

    def test_put_no_active_session_raises(self, tmp_path: Path):
        """Put without active session raises error."""
        model_file = tmp_path / "model.pt"
        model_file.write_bytes(b"data")

        backend = MemoryBackend(bucket="bucket", prefix="")
        mock_db = Mock()
        mock_db.sessions = Mock()
        mock_db.sessions.get_active.return_value = None

        glaas_deps = create_mock_glaas_deps()

        with patch("roar.services.put.service.get_glaas_url", return_value="http://glaas.test"):
            service = PutService(
                db_context=mock_db,
                backend=backend,
                destination="memory://test-bucket/models",
                repo_root=tmp_path,
                **glaas_deps,
            )

            with pytest.raises(ValueError, match=r"No active session"):
                service.put(sources=[str(model_file)], message="test")

    def test_put_missing_file_raises(self, tmp_path: Path):
        """Put with missing file raises FileNotFoundError."""
        backend = MemoryBackend(bucket="bucket", prefix="")
        mock_db = Mock()
        mock_db.sessions = Mock()
        mock_db.sessions.get_active.return_value = {"id": 1}

        glaas_deps = create_mock_glaas_deps()

        with patch("roar.services.put.service.get_glaas_url", return_value="http://glaas.test"):
            service = PutService(
                db_context=mock_db,
                backend=backend,
                destination="memory://test-bucket/models",
                repo_root=tmp_path,
                **glaas_deps,
            )

            with pytest.raises(FileNotFoundError):
                service.put(sources=[str(tmp_path / "missing.pt")], message="test")
