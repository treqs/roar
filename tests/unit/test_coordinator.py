"""
Unit tests for RegistrationCoordinator.

Tests that register_lineage delegates to batch job creation (Phase 2)
and correctly orchestrates the 4-phase registration pattern.
"""

from unittest.mock import MagicMock

import pytest

from roar.core.interfaces.registration import (
    ArtifactRegistrationResult,
    GitContext,
    JobLinkResult,
    JobRegistrationResult,
)
from roar.integrations.glaas.registration.coordinator import RegistrationCoordinator


class TestRegisterLineage:
    """Tests for RegistrationCoordinator.register_lineage()."""

    @pytest.fixture
    def mock_session_service(self):
        return MagicMock()

    @pytest.fixture
    def mock_artifact_service(self):
        service = MagicMock()
        service.resolve_artifact_hash.side_effect = lambda ref: (
            f"id-{ref.get('hash', 'unknown')}",
            None,
        )
        return service

    @pytest.fixture
    def mock_job_service(self):
        return MagicMock()

    @pytest.fixture
    def mock_logger(self):
        return MagicMock()

    @pytest.fixture
    def coordinator(
        self, mock_session_service, mock_artifact_service, mock_job_service, mock_logger
    ):
        return RegistrationCoordinator(
            session_service=mock_session_service,
            artifact_service=mock_artifact_service,
            job_service=mock_job_service,
            logger=mock_logger,
        )

    def test_phase2_uses_batch_call(self, coordinator, mock_job_service):
        """Phase 2 uses create_jobs_batch instead of per-job calls."""
        mock_job_service.create_jobs_batch.return_value = [
            JobRegistrationResult(success=True, job_uid="job-001"),
            JobRegistrationResult(success=True, job_uid="job-002"),
        ]
        mock_artifact_service = coordinator.artifact_service
        mock_artifact_service.register_batch.return_value = ArtifactRegistrationResult(
            success_count=1, error_count=0, errors=[]
        )

        jobs = [
            {"job_uid": "job-001", "command": "train.py"},
            {"job_uid": "job-002", "command": "eval.py"},
        ]
        artifacts = [{"hash": "abc123", "size": 1024}]

        result = coordinator.register_lineage(
            session_hash="session123",
            git_context=GitContext(repo="repo", commit="abc", branch="main"),
            jobs=jobs,
            artifacts=artifacts,
        )

        # Should call batch, not individual create_job
        mock_job_service.create_jobs_batch.assert_called_once_with(
            jobs=jobs,
            session_hash="session123",
            git_context=GitContext(repo="repo", commit="abc", branch="main"),
        )
        assert result.jobs_created == 2
        assert result.jobs_failed == 0

    def test_phase2_counts_failures(self, coordinator, mock_job_service):
        """Failed jobs in batch are counted correctly."""
        mock_job_service.create_jobs_batch.return_value = [
            JobRegistrationResult(success=True, job_uid="job-001"),
            JobRegistrationResult(success=False, job_uid="job-002", error="duplicate"),
        ]

        jobs = [
            {"job_uid": "job-001", "command": "train.py"},
            {"job_uid": "job-002", "command": "eval.py"},
        ]

        result = coordinator.register_lineage(
            session_hash="session123",
            git_context=GitContext(repo="repo", commit="abc", branch="main"),
            jobs=jobs,
            artifacts=[],
        )

        assert result.jobs_created == 1
        assert result.jobs_failed == 1
        assert any("job-002" in e for e in result.errors)

    def test_empty_jobs_skips_batch(self, coordinator, mock_job_service):
        """Empty jobs list does not call batch endpoint."""
        result = coordinator.register_lineage(
            session_hash="session123",
            git_context=GitContext(repo="repo", commit="abc", branch="main"),
            jobs=[],
            artifacts=[],
        )

        mock_job_service.create_jobs_batch.assert_not_called()
        assert result.jobs_created == 0
        assert result.jobs_failed == 0

    def test_phase4_only_links_successfully_created_jobs(self, coordinator, mock_job_service):
        """Phase 4 only links artifacts for jobs that succeeded in Phase 2."""
        mock_job_service.create_jobs_batch.return_value = [
            JobRegistrationResult(success=True, job_uid="job-001"),
            JobRegistrationResult(success=False, job_uid="job-002", error="failed"),
        ]
        mock_job_service.link_job_artifacts.return_value = JobLinkResult(
            success=True, job_uid="job-001", inputs_linked=1, outputs_linked=1
        )

        jobs = [
            {
                "job_uid": "job-001",
                "_inputs": [{"hash": "in1", "path": "/data/input.csv"}],
                "_outputs": [{"hash": "out1", "path": "/output/model.pt"}],
            },
            {
                "job_uid": "job-002",
                "_inputs": [{"hash": "in2", "path": "/data/other.csv"}],
                "_outputs": [],
            },
        ]

        result = coordinator.register_lineage(
            session_hash="session123",
            git_context=GitContext(repo="repo", commit="abc", branch="main"),
            jobs=jobs,
            artifacts=[],
        )

        # link_job_artifacts should only be called for job-001 (succeeded)
        mock_job_service.link_job_artifacts.assert_called_once()
        call_kwargs = mock_job_service.link_job_artifacts.call_args[1]
        assert call_kwargs["job_uid"] == "job-001"
        assert call_kwargs["inputs"][0]["artifact_hash"] == "id-in1"
        assert call_kwargs["outputs"][0]["artifact_hash"] == "id-out1"
        assert result.links_created == 2  # 1 input + 1 output

    def test_full_4_phase_flow(self, coordinator, mock_job_service, mock_artifact_service):
        """Full 4-phase flow: session → batch jobs → artifacts → links."""
        # Phase 2: batch job creation
        mock_job_service.create_jobs_batch.return_value = [
            JobRegistrationResult(success=True, job_uid="job-001"),
        ]

        # Phase 3: artifact registration
        mock_artifact_service.register_batch.return_value = ArtifactRegistrationResult(
            success_count=2, error_count=0, errors=[]
        )

        # Phase 4: link artifacts
        mock_job_service.link_job_artifacts.return_value = JobLinkResult(
            success=True, job_uid="job-001", inputs_linked=1, outputs_linked=1
        )

        jobs = [
            {
                "job_uid": "job-001",
                "_inputs": [{"hash": "in1", "path": "/data/input.csv"}],
                "_outputs": [{"hash": "out1", "path": "/output/model.pt"}],
            },
        ]
        artifacts = [
            {"hash": "in1", "size": 100},
            {"hash": "out1", "size": 200},
        ]

        result = coordinator.register_lineage(
            session_hash="session123",
            git_context=GitContext(repo="repo", commit="abc", branch="main"),
            jobs=jobs,
            artifacts=artifacts,
        )

        assert result.session_registered is True
        assert result.jobs_created == 1
        assert result.jobs_failed == 0
        assert result.artifacts_registered == 2
        assert result.artifacts_failed == 0
        assert result.links_created == 2
        assert result.links_failed == 0
        assert result.errors == []

    def test_link_failure_counted(self, coordinator, mock_job_service):
        """Link failures are tracked in the result."""
        mock_job_service.create_jobs_batch.return_value = [
            JobRegistrationResult(success=True, job_uid="job-001"),
        ]
        mock_job_service.link_job_artifacts.return_value = JobLinkResult(
            success=False,
            job_uid="job-001",
            inputs_linked=0,
            outputs_linked=0,
            error="artifact not found",
        )

        jobs = [
            {
                "job_uid": "job-001",
                "_inputs": [{"hash": "in1", "path": "/data/input.csv"}],
            },
        ]

        result = coordinator.register_lineage(
            session_hash="session123",
            git_context=GitContext(repo="repo", commit="abc", branch="main"),
            jobs=jobs,
            artifacts=[],
        )

        assert result.links_failed == 1
        assert any("job-001" in e for e in result.errors)


class TestRegisterLineageUnderRegistrationSession:
    """Tests for the bearer (registration-session) coordinator.

    The bearer flow originally lacked a Phase 3 step, so artifacts only came
    into existence via Phase 4 link requests with `size: BigInt(?? 0)` —
    producing the 247 0-byte stubs on the M1 nanochat DAG. These tests pin
    the post-fix invariant: when artifacts are provided, Phase 3 runs
    between Phase 2 and Phase 4.
    """

    @pytest.fixture
    def mock_session_service(self):
        return MagicMock()

    @pytest.fixture
    def mock_artifact_service(self):
        service = MagicMock()
        service.register_batch_under_registration_session.return_value = ArtifactRegistrationResult(
            success_count=2, error_count=0, errors=[]
        )
        return service

    @pytest.fixture
    def mock_job_service(self):
        service = MagicMock()
        service.create_jobs_batch_under_registration_session.return_value = (
            [JobRegistrationResult(success=True, job_uid="job-001")],
            {"created": 1, "existing": 0},
        )
        service.link_job_artifacts_under_registration_session.return_value = JobLinkResult(
            success=True,
            job_uid="job-001",
            artifacts_registered=2,
            inputs_linked=1,
            outputs_linked=1,
        )
        return service

    @pytest.fixture
    def mock_logger(self):
        return MagicMock()

    @pytest.fixture
    def coordinator(
        self, mock_session_service, mock_artifact_service, mock_job_service, mock_logger
    ):
        return RegistrationCoordinator(
            session_service=mock_session_service,
            artifact_service=mock_artifact_service,
            job_service=mock_job_service,
            logger=mock_logger,
        )

    def test_phase3_runs_with_artifacts(self, coordinator, mock_artifact_service, mock_job_service):
        """Artifacts are staged via Phase 3 before Phase 4 link runs."""
        jobs = [
            {
                "job_uid": "job-001",
                "_inputs": [{"hash": "in1", "path": "/in.bin"}],
                "_outputs": [{"hash": "out1", "path": "/out.bin"}],
            },
        ]
        artifacts = [
            {
                "hashes": [{"algorithm": "blake3", "digest": "in1"}],
                "size": 100,
                "source_type": None,
            },
            {
                "hashes": [{"algorithm": "blake3", "digest": "out1"}],
                "size": 200,
                "source_type": None,
            },
        ]

        result = coordinator.register_lineage_under_registration_session(
            registration_session_id="reg-sess-1",
            git_context=GitContext(repo="repo", commit="abc", branch="main"),
            jobs=jobs,
            artifacts=artifacts,
        )

        mock_artifact_service.register_batch_under_registration_session.assert_called_once_with(
            artifacts, "reg-sess-1"
        )
        assert result.artifacts_registered >= 2

    def test_phase3_skipped_when_no_artifacts(self, coordinator, mock_artifact_service):
        """Backwards-compat: omitted artifacts ⇒ no Phase 3 call."""
        coordinator.register_lineage_under_registration_session(
            registration_session_id="reg-sess-1",
            git_context=GitContext(repo="repo", commit="abc", branch="main"),
            jobs=[{"job_uid": "job-001"}],
        )

        mock_artifact_service.register_batch_under_registration_session.assert_not_called()

    def test_phase3_errors_collected_but_does_not_block_phase4(
        self, coordinator, mock_artifact_service, mock_job_service
    ):
        """Phase 3 failures surface in errors[] but Phase 4 still runs.

        Matches the standard ``register_lineage`` behavior: Phase 4 plows
        ahead regardless of Phase 3 outcome. Combined with the upcoming
        glaas-api change that flips the link endpoints to strict (no
        stub-create), a Phase 3 failure would then produce a loud 404 on
        Phase 4 rather than the silent stub.
        """
        mock_artifact_service.register_batch_under_registration_session.return_value = (
            ArtifactRegistrationResult(
                success_count=0, error_count=1, errors=["Boom: validation failed"]
            )
        )

        jobs = [
            {
                "job_uid": "job-001",
                "_inputs": [{"hash": "in1", "path": "/in.bin"}],
            },
        ]
        artifacts = [
            {"hashes": [{"algorithm": "blake3", "digest": "in1"}], "size": 100, "source_type": None}
        ]

        result = coordinator.register_lineage_under_registration_session(
            registration_session_id="reg-sess-1",
            git_context=GitContext(repo="repo", commit="abc", branch="main"),
            jobs=jobs,
            artifacts=artifacts,
        )

        assert any("Boom" in e for e in result.errors)
        mock_job_service.link_job_artifacts_under_registration_session.assert_called_once()
