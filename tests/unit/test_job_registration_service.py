"""
Unit tests for JobRegistrationService.

Tests create_jobs_batch(), create_job(), and link_job_artifacts()
using mocked GlaasClient dependency.
"""

from unittest.mock import MagicMock

import pytest

from roar.core.interfaces.registration import (
    GitContext,
)
from roar.services.registration.job import JobRegistrationService


class TestCreateJobsBatch:
    """Tests for JobRegistrationService.create_jobs_batch()."""

    @pytest.fixture
    def mock_client(self):
        """Create a mock GLaaS client."""
        return MagicMock()

    @pytest.fixture
    def mock_logger(self):
        """Create a mock logger."""
        logger = MagicMock()
        return logger

    @pytest.fixture
    def service(self, mock_client, mock_logger):
        """Create a JobRegistrationService with mocked dependencies."""
        return JobRegistrationService(
            client=mock_client,
            logger=mock_logger,
        )

    def _make_job(self, job_uid="job-001", **overrides):
        """Helper to create a valid job dict."""
        base = {
            "job_uid": job_uid,
            "command": "python train.py",
            "timestamp": 1700000000.0,
            "git_commit": "abc123def456",
            "git_branch": "main",
            "duration_seconds": 60.0,
            "exit_code": 0,
            "job_type": "run",
            "step_number": 1,
        }
        base.update(overrides)
        return base

    def test_empty_jobs_returns_empty_list(self, service):
        """Empty input returns empty list without calling client."""
        result = service.create_jobs_batch(
            jobs=[],
            session_hash="session123",
            git_context=GitContext(repo="repo", commit="abc", branch="main"),
        )
        assert result == []
        service.client.register_jobs_batch.assert_not_called()

    def test_happy_path_single_job(self, service, mock_client):
        """Single valid job is sent to batch endpoint and result returned."""
        mock_client.register_jobs_batch.return_value = (["server-id-1"], [], None)

        jobs = [self._make_job("job-001")]
        results = service.create_jobs_batch(
            jobs=jobs,
            session_hash="session123",
            git_context=GitContext(repo="repo", commit="abc", branch="main"),
        )

        assert len(results) == 1
        assert results[0].success is True
        assert results[0].job_uid == "job-001"
        assert results[0].job_id == "server-id-1"
        mock_client.register_jobs_batch.assert_called_once()

    def test_happy_path_multiple_jobs(self, service, mock_client):
        """Multiple valid jobs are batched into a single request."""
        mock_client.register_jobs_batch.return_value = (
            ["id-1", "id-2", "id-3"],
            [],
            None,
        )

        jobs = [
            self._make_job("job-001", step_number=1),
            self._make_job("job-002", step_number=2),
            self._make_job("job-003", step_number=3),
        ]
        results = service.create_jobs_batch(
            jobs=jobs,
            session_hash="session123",
            git_context=GitContext(repo="repo", commit="abc", branch="main"),
        )

        assert len(results) == 3
        assert all(r.success for r in results)
        # Should be a single batch call, not 3 individual calls
        mock_client.register_jobs_batch.assert_called_once()

    def test_missing_job_uid_returns_failure(self, service, mock_client):
        """Job without job_uid gets a failure result without hitting server."""
        mock_client.register_jobs_batch.return_value = (["id-1"], [], None)

        jobs = [
            {"command": "echo hi", "timestamp": 1700000000.0},  # no job_uid
            self._make_job("job-002"),
        ]
        results = service.create_jobs_batch(
            jobs=jobs,
            session_hash="session123",
            git_context=GitContext(repo="repo", commit="abc", branch="main"),
        )

        assert len(results) == 2
        # First job failed locally
        assert results[0].success is False
        assert "job_uid" in results[0].error.lower()
        # Second job succeeded via batch
        assert results[1].success is True

    def test_validation_failure_returns_error(self, service, mock_client):
        """Job that fails validation returns error without hitting server."""
        mock_client.register_jobs_batch.return_value = ([], [], None)

        jobs = [
            self._make_job("job-001", command=""),  # empty command fails validation
        ]
        results = service.create_jobs_batch(
            jobs=jobs,
            session_hash="session123",
            git_context=GitContext(repo="repo", commit="abc", branch="main"),
        )

        assert len(results) == 1
        assert results[0].success is False
        assert results[0].job_uid == "job-001"
        # Client should not be called since all jobs failed validation
        mock_client.register_jobs_batch.assert_not_called()

    def test_overall_batch_error_marks_all_failed(self, service, mock_client):
        """When server returns overall error, all jobs are marked failed."""
        mock_client.register_jobs_batch.return_value = (
            [],
            [],
            "Server unavailable",
        )

        jobs = [
            self._make_job("job-001"),
            self._make_job("job-002"),
        ]
        results = service.create_jobs_batch(
            jobs=jobs,
            session_hash="session123",
            git_context=GitContext(repo="repo", commit="abc", branch="main"),
        )

        assert len(results) == 2
        assert all(not r.success for r in results)
        assert all(r.error == "Server unavailable" for r in results)

    def test_per_job_error_from_server(self, service, mock_client):
        """Server-side per-job errors are mapped back correctly."""
        mock_client.register_jobs_batch.return_value = (
            [None, "id-2"],
            ["duplicate job_uid", ""],
            None,
        )

        jobs = [
            self._make_job("job-001"),
            self._make_job("job-002"),
        ]
        results = service.create_jobs_batch(
            jobs=jobs,
            session_hash="session123",
            git_context=GitContext(repo="repo", commit="abc", branch="main"),
        )

        assert len(results) == 2
        assert results[0].success is False
        assert "duplicate" in results[0].error
        assert results[1].success is True
        assert results[1].job_id == "id-2"

    def test_git_context_fallback(self, service, mock_client):
        """Jobs missing git fields fall back to git_context values."""
        mock_client.register_jobs_batch.return_value = (["id-1"], [], None)

        # Job without git_commit or git_branch
        jobs = [
            {
                "job_uid": "job-001",
                "command": "python train.py",
                "timestamp": 1700000000.0,
                "duration_seconds": 60.0,
                "exit_code": 0,
                "job_type": "run",
                "step_number": 1,
            }
        ]
        results = service.create_jobs_batch(
            jobs=jobs,
            session_hash="session123",
            git_context=GitContext(
                repo="https://github.com/test/repo",
                commit="fallback_commit",
                branch="fallback_branch",
            ),
        )

        assert len(results) == 1
        assert results[0].success is True

        # Verify the payload sent to server has fallback values
        call_args = mock_client.register_jobs_batch.call_args
        sent_jobs = call_args[1]["jobs"] if "jobs" in call_args[1] else call_args[0][1]
        assert sent_jobs[0]["git_commit"] == "fallback_commit"
        assert sent_jobs[0]["git_branch"] == "fallback_branch"

    def test_secret_filtering_applied(self, service, mock_client):
        """When a secret filter is provided, job data is filtered."""
        mock_filter = MagicMock()
        mock_filter.filter_command.return_value = ("REDACTED", ["secret-1"])
        mock_filter.filter_git_url.return_value = ("https://filtered", [])
        mock_filter.filter_metadata.return_value = ({}, [])

        service_with_filter = JobRegistrationService(
            client=mock_client,
            secret_filter=mock_filter,
        )

        mock_client.register_jobs_batch.return_value = (["id-1"], [], None)

        jobs = [self._make_job("job-001", command="secret-token python train.py")]
        results = service_with_filter.create_jobs_batch(
            jobs=jobs,
            session_hash="session123",
            git_context=GitContext(repo="repo", commit="abc", branch="main"),
        )

        assert len(results) == 1
        assert results[0].success is True
        mock_filter.filter_command.assert_called_once()

        # Verify filtered command was sent
        call_args = mock_client.register_jobs_batch.call_args
        sent_jobs = call_args[1]["jobs"] if "jobs" in call_args[1] else call_args[0][1]
        assert sent_jobs[0]["command"] == "REDACTED"

    def test_metadata_included_in_payload(self, service, mock_client):
        """Metadata is included in the payload when present."""
        mock_client.register_jobs_batch.return_value = (["id-1"], [], None)

        jobs = [self._make_job("job-001", metadata='{"env": "prod"}')]
        results = service.create_jobs_batch(
            jobs=jobs,
            session_hash="session123",
            git_context=GitContext(repo="repo", commit="abc", branch="main"),
        )

        assert results[0].success is True
        call_args = mock_client.register_jobs_batch.call_args
        sent_jobs = call_args[1]["jobs"] if "jobs" in call_args[1] else call_args[0][1]
        assert sent_jobs[0]["metadata"] == '{"env": "prod"}'

    def test_mixed_valid_and_invalid_preserves_order(self, service, mock_client):
        """Mix of valid and invalid jobs preserves original ordering."""
        mock_client.register_jobs_batch.return_value = (["id-2"], [], None)

        jobs = [
            self._make_job("job-001", command=""),  # invalid: empty command
            self._make_job("job-002"),  # valid
            {},  # invalid: no job_uid
        ]
        results = service.create_jobs_batch(
            jobs=jobs,
            session_hash="session123",
            git_context=GitContext(repo="repo", commit="abc", branch="main"),
        )

        assert len(results) == 3
        assert results[0].success is False  # validation failed
        assert results[0].job_uid == "job-001"
        assert results[1].success is True  # batch succeeded
        assert results[1].job_uid == "job-002"
        assert results[2].success is False  # missing job_uid
