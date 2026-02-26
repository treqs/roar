"""
Tests for roar put GLaaS registration integration.

roar put ALWAYS registers lineage with GLaaS:
1. Collects lineage for each uploaded artifact via LineageCollector
2. Merges lineages (deduplicates shared jobs/artifacts)
3. Registers via RegistrationCoordinator.register_lineage()
4. Fails if GLaaS is not configured
"""

from unittest.mock import MagicMock, patch

import pytest

from roar.core.interfaces.registration import (
    BatchRegistrationResult,
    GitContext,
    JobLinkResult,
    JobRegistrationResult,
    SessionRegistrationResult,
)
from roar.core.interfaces.upload import LineageData
from roar.services.put.service import PutService


def create_mock_session_service():
    """Create mock session service that returns success."""
    mock = MagicMock()
    mock.compute_session_hash.return_value = "session_hash_abc123"
    mock.register.return_value = SessionRegistrationResult(
        success=True,
        session_hash="session_hash_abc123",
        session_url="https://glaas.ai/dag/session_hash_abc123",
    )
    return mock


def create_mock_glaas_client():
    """Create mock GLaaS client."""
    mock = MagicMock()
    mock.health_check.return_value = True
    return mock


class TestPutRegistrationIntegration:
    """Tests for PutService GLaaS registration."""

    @pytest.fixture
    def mock_db_context(self):
        """Create mock database context."""
        db = MagicMock()
        db.sessions.get_active.return_value = {"id": 1, "hash": "session_abc123"}
        db.sessions.get_next_step_number.return_value = 1
        db.artifacts.register.return_value = ("artifact_1", True)
        db.jobs.create.return_value = (1, "job_uid_123")
        return db

    @pytest.fixture
    def mock_backend(self):
        """Create mock storage backend."""
        backend = MagicMock()
        backend.upload.return_value = "s3://bucket/test.pt"
        return backend

    @pytest.fixture
    def temp_file(self, tmp_path):
        """Create a temp file to upload."""
        f = tmp_path / "model.pt"
        f.write_bytes(b"model data")
        return f

    def test_put_collects_lineage_for_uploaded_artifacts(
        self, mock_db_context, mock_backend, temp_file, tmp_path
    ):
        """PutService should collect lineage for each uploaded artifact."""
        mock_lineage = LineageData(
            jobs=[{"id": 1, "job_uid": "job_1", "command": "train.py"}],
            artifacts=[{"hash": "abc123", "size": 100}],
            artifact_hashes={"abc123"},
            pipeline={"id": 1},
        )

        mock_result = BatchRegistrationResult(
            session_registered=True,
            jobs_created=1,
            jobs_failed=0,
            artifacts_registered=1,
            artifacts_failed=0,
            links_created=2,
            links_failed=0,
            errors=[],
        )

        with patch("roar.services.put.service.get_glaas_url", return_value="http://glaas.test"):
            service = PutService(
                db_context=mock_db_context,
                backend=mock_backend,
                destination="s3://bucket/prefix",
                repo_root=tmp_path,
                glaas_client=create_mock_glaas_client(),
                session_service=create_mock_session_service(),
            )

            with (
                patch("roar.services.put.service.LineageCollector") as MockLineageCollector,
                patch("roar.services.put.service.RegistrationCoordinator") as MockCoordinator,
            ):
                mock_collector = MagicMock()
                mock_collector.collect.return_value = mock_lineage
                MockLineageCollector.return_value = mock_collector

                mock_coordinator = MagicMock()
                mock_coordinator.register_lineage.return_value = mock_result
                MockCoordinator.return_value = mock_coordinator

                service.put(
                    sources=[str(temp_file)],
                    message="test upload",
                )

                # Verify LineageCollector.collect was called with artifact hashes
                mock_collector.collect.assert_called_once()
                call_args = mock_collector.collect.call_args
                # First arg should be list of artifact hashes
                artifact_hashes = call_args[0][0]
                assert isinstance(artifact_hashes, list)
                assert len(artifact_hashes) > 0

    def test_put_registers_lineage_via_coordinator(
        self, mock_db_context, mock_backend, temp_file, tmp_path
    ):
        """PutService should register lineage via RegistrationCoordinator."""
        mock_lineage = LineageData(
            jobs=[{"id": 1, "job_uid": "job_1", "command": "train.py"}],
            artifacts=[{"hash": "abc123", "size": 100}],
            artifact_hashes={"abc123"},
            pipeline={"id": 1},
        )

        mock_result = BatchRegistrationResult(
            session_registered=True,
            jobs_created=1,
            jobs_failed=0,
            artifacts_registered=1,
            artifacts_failed=0,
            links_created=2,
            links_failed=0,
            errors=[],
        )

        with patch("roar.services.put.service.get_glaas_url", return_value="http://glaas.test"):
            service = PutService(
                db_context=mock_db_context,
                backend=mock_backend,
                destination="s3://bucket/prefix",
                repo_root=tmp_path,
                glaas_client=create_mock_glaas_client(),
                session_service=create_mock_session_service(),
            )

            with (
                patch("roar.services.put.service.LineageCollector") as MockLineageCollector,
                patch("roar.services.put.service.RegistrationCoordinator") as MockCoordinator,
            ):
                mock_collector = MagicMock()
                mock_collector.collect.return_value = mock_lineage
                MockLineageCollector.return_value = mock_collector

                mock_coordinator = MagicMock()
                mock_coordinator.register_lineage.return_value = mock_result
                MockCoordinator.return_value = mock_coordinator

                service.put(
                    sources=[str(temp_file)],
                    message="test upload",
                )

                # Verify RegistrationCoordinator.register_lineage was called
                mock_coordinator.register_lineage.assert_called_once()
                call_args = mock_coordinator.register_lineage.call_args
                # Should have session_hash, git_context, jobs, artifacts
                assert "session_hash" in call_args.kwargs or len(call_args.args) >= 1

    def test_put_fails_if_glaas_not_configured(
        self, mock_db_context, mock_backend, temp_file, tmp_path
    ):
        """PutService should fail if GLaaS is not configured."""
        service = PutService(
            db_context=mock_db_context,
            backend=mock_backend,
            destination="s3://bucket/prefix",
            repo_root=tmp_path,
        )

        with (
            patch("roar.services.put.service.get_glaas_url", return_value=None),
            pytest.raises(ValueError, match=r"GLaaS.*not configured"),
        ):
            service.put(
                sources=[str(temp_file)],
                message="test upload",
            )

    def test_put_merges_lineage_for_multiple_artifacts(
        self, mock_db_context, mock_backend, tmp_path
    ):
        """PutService should pass all artifact hashes to LineageCollector for merging."""
        # Create multiple temp files
        file1 = tmp_path / "model1.pt"
        file1.write_bytes(b"model 1 data")
        file2 = tmp_path / "model2.pt"
        file2.write_bytes(b"model 2 data")

        # Make register return different artifact IDs for each file
        artifact_ids = iter(["artifact_1", "artifact_2"])
        mock_db_context.artifacts.register.side_effect = lambda **kw: (
            next(artifact_ids),
            True,
        )

        mock_lineage = LineageData(
            jobs=[
                {"id": 1, "job_uid": "job_1", "command": "train1.py"},
                {"id": 2, "job_uid": "job_2", "command": "train2.py"},
            ],
            artifacts=[
                {"hash": "hash1", "size": 100},
                {"hash": "hash2", "size": 200},
            ],
            artifact_hashes={"hash1", "hash2"},
            pipeline={"id": 1},
        )

        mock_result = BatchRegistrationResult(
            session_registered=True,
            jobs_created=2,
            jobs_failed=0,
            artifacts_registered=2,
            artifacts_failed=0,
            links_created=4,
            links_failed=0,
            errors=[],
        )

        with patch("roar.services.put.service.get_glaas_url", return_value="http://glaas.test"):
            service = PutService(
                db_context=mock_db_context,
                backend=mock_backend,
                destination="s3://bucket/prefix",
                repo_root=tmp_path,
                glaas_client=create_mock_glaas_client(),
                session_service=create_mock_session_service(),
            )

            with (
                patch("roar.services.put.service.LineageCollector") as MockLineageCollector,
                patch("roar.services.put.service.RegistrationCoordinator") as MockCoordinator,
            ):
                mock_collector = MagicMock()
                mock_collector.collect.return_value = mock_lineage
                MockLineageCollector.return_value = mock_collector

                mock_coordinator = MagicMock()
                mock_coordinator.register_lineage.return_value = mock_result
                MockCoordinator.return_value = mock_coordinator

                service.put(
                    sources=[str(file1), str(file2)],
                    message="test upload multiple",
                )

                # Verify collect was called with BOTH artifact hashes
                mock_collector.collect.assert_called_once()
                call_args = mock_collector.collect.call_args
                artifact_hashes = call_args[0][0]
                assert len(artifact_hashes) == 2

    def test_put_registers_put_job_with_glaas(
        self, mock_db_context, mock_backend, temp_file, tmp_path
    ):
        """PutService should register the put job itself with GLaaS (not just upstream lineage)."""
        mock_lineage = LineageData(
            jobs=[{"id": 1, "job_uid": "job_1", "command": "train.py"}],
            artifacts=[{"hash": "abc123", "size": 100}],
            artifact_hashes={"abc123"},
            pipeline={"id": 1},
        )

        mock_result = BatchRegistrationResult(
            session_registered=True,
            jobs_created=1,
            jobs_failed=0,
            artifacts_registered=1,
            artifacts_failed=0,
            links_created=2,
            links_failed=0,
            errors=[],
        )

        with patch("roar.services.put.service.get_glaas_url", return_value="http://glaas.test"):
            service = PutService(
                db_context=mock_db_context,
                backend=mock_backend,
                destination="s3://bucket/prefix",
                repo_root=tmp_path,
                glaas_client=create_mock_glaas_client(),
                session_service=create_mock_session_service(),
            )

            with (
                patch("roar.services.put.service.LineageCollector") as MockLineageCollector,
                patch("roar.services.put.service.RegistrationCoordinator") as MockCoordinator,
            ):
                mock_collector = MagicMock()
                mock_collector.collect.return_value = mock_lineage
                MockLineageCollector.return_value = mock_collector

                mock_coordinator = MagicMock()
                mock_coordinator.register_lineage.return_value = mock_result
                # Set up job_service mocks for put job registration
                mock_coordinator.job_service.create_job.return_value = JobRegistrationResult(
                    success=True,
                    job_uid="job_uid_123",
                    job_id="42",
                )
                mock_coordinator.job_service.link_job_artifacts.return_value = JobLinkResult(
                    success=True,
                    job_uid="job_uid_123",
                    inputs_linked=1,
                    outputs_linked=0,
                )
                mock_coordinator.artifact_service.resolve_artifact_hash.side_effect = lambda ref: (
                    "resolved-art-1",
                    None,
                )
                MockCoordinator.return_value = mock_coordinator

                result = service.put(
                    sources=[str(temp_file)],
                    message="publish model",
                    git_commit="abc123def",
                    git_tag="roar/abc123de",
                )

                assert result.success

                # Verify the put job itself was registered with GLaaS
                mock_coordinator.job_service.create_job.assert_called_once()
                create_kwargs = mock_coordinator.job_service.create_job.call_args.kwargs
                assert create_kwargs["job_type"] == "put"
                assert create_kwargs["session_hash"] == "session_hash_abc123"
                assert create_kwargs["job_uid"] == "job_uid_123"
                assert create_kwargs["exit_code"] == 0

                # Verify uploaded artifacts were linked as inputs to the put job
                mock_coordinator.job_service.link_job_artifacts.assert_called_once()
                link_kwargs = mock_coordinator.job_service.link_job_artifacts.call_args.kwargs
                assert link_kwargs["job_uid"] == "job_uid_123"
                assert len(link_kwargs["inputs"]) == 1
                assert link_kwargs["outputs"] == []

    def test_put_includes_git_context_in_registration(
        self, mock_db_context, mock_backend, temp_file, tmp_path
    ):
        """PutService should include git commit and tag in registration."""
        mock_lineage = LineageData(
            jobs=[],
            artifacts=[],
            artifact_hashes=set(),
            pipeline={"id": 1},
        )

        mock_result = BatchRegistrationResult(
            session_registered=True,
            jobs_created=0,
            jobs_failed=0,
            artifacts_registered=0,
            artifacts_failed=0,
            links_created=0,
            links_failed=0,
            errors=[],
        )

        with patch("roar.services.put.service.get_glaas_url", return_value="http://glaas.test"):
            service = PutService(
                db_context=mock_db_context,
                backend=mock_backend,
                destination="s3://bucket/prefix",
                repo_root=tmp_path,
                glaas_client=create_mock_glaas_client(),
                session_service=create_mock_session_service(),
            )

            with (
                patch("roar.services.put.service.LineageCollector") as MockLineageCollector,
                patch("roar.services.put.service.RegistrationCoordinator") as MockCoordinator,
            ):
                mock_collector = MagicMock()
                mock_collector.collect.return_value = mock_lineage
                MockLineageCollector.return_value = mock_collector

                mock_coordinator = MagicMock()
                mock_coordinator.register_lineage.return_value = mock_result
                MockCoordinator.return_value = mock_coordinator

                service.put(
                    sources=[str(temp_file)],
                    message="test upload",
                    git_commit="abc123def",
                    git_tag="roar/abc123de",
                )

                # Verify git context was passed to register_lineage
                call_args = mock_coordinator.register_lineage.call_args
                if call_args.kwargs:
                    git_context = call_args.kwargs.get("git_context")
                else:
                    # Positional args: session_hash, git_context, jobs, artifacts
                    git_context = call_args.args[1] if len(call_args.args) > 1 else None

                assert git_context is not None
                assert isinstance(git_context, GitContext)
                assert git_context.commit == "abc123def"


class TestPutRegistrationErrors:
    """Tests for registration error handling."""

    @pytest.fixture
    def mock_db_context(self):
        """Create mock database context."""
        db = MagicMock()
        db.sessions.get_active.return_value = {"id": 1, "hash": "session_abc123"}
        db.sessions.get_next_step_number.return_value = 1
        db.artifacts.register.return_value = ("artifact_1", True)
        db.jobs.create.return_value = (1, "job_uid_123")
        return db

    @pytest.fixture
    def mock_backend(self):
        """Create mock storage backend."""
        backend = MagicMock()
        backend.upload.return_value = "s3://bucket/test.pt"
        return backend

    @pytest.fixture
    def temp_file(self, tmp_path):
        """Create a temp file to upload."""
        f = tmp_path / "model.pt"
        f.write_bytes(b"model data")
        return f

    def test_put_propagates_registration_errors(
        self, mock_db_context, mock_backend, temp_file, tmp_path
    ):
        """PutService should propagate registration errors."""
        mock_lineage = LineageData(
            jobs=[{"id": 1, "job_uid": "job_1"}],
            artifacts=[],
            artifact_hashes=set(),
            pipeline={"id": 1},
        )

        mock_result = BatchRegistrationResult(
            session_registered=True,
            jobs_created=0,
            jobs_failed=1,
            artifacts_registered=0,
            artifacts_failed=0,
            links_created=0,
            links_failed=0,
            errors=["Job registration failed: connection error"],
        )

        with patch("roar.services.put.service.get_glaas_url", return_value="http://glaas.test"):
            service = PutService(
                db_context=mock_db_context,
                backend=mock_backend,
                destination="s3://bucket/prefix",
                repo_root=tmp_path,
                glaas_client=create_mock_glaas_client(),
                session_service=create_mock_session_service(),
            )

            with (
                patch("roar.services.put.service.LineageCollector") as MockLineageCollector,
                patch("roar.services.put.service.RegistrationCoordinator") as MockCoordinator,
            ):
                mock_collector = MagicMock()
                mock_collector.collect.return_value = mock_lineage
                MockLineageCollector.return_value = mock_collector

                mock_coordinator = MagicMock()
                mock_coordinator.register_lineage.return_value = mock_result
                MockCoordinator.return_value = mock_coordinator

                # Should raise or return error result
                result = service.put(
                    sources=[str(temp_file)],
                    message="test upload",
                )

                # Either raises exception or returns result with error
                assert not result.success or result.error is not None

    def test_put_fails_on_glaas_health_check_failure(
        self, mock_db_context, mock_backend, temp_file, tmp_path
    ):
        """PutService should fail if GLaaS health check fails."""
        service = PutService(
            db_context=mock_db_context,
            backend=mock_backend,
            destination="s3://bucket/prefix",
            repo_root=tmp_path,
        )

        with (
            patch("roar.services.put.service.get_glaas_url", return_value="http://glaas.test"),
            patch("roar.services.put.service.GlaasClient") as MockClient,
        ):
            mock_client = MagicMock()
            mock_client.health_check.side_effect = Exception("Connection refused")
            MockClient.return_value = mock_client

            with pytest.raises(Exception, match=r"Connection refused|GLaaS"):
                service.put(
                    sources=[str(temp_file)],
                    message="test upload",
                )
