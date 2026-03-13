"""
Unit tests for RegisterService.

Tests error conditions and dry-run mode using mocked dependencies.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from roar.services.registration.register_service import RegisterResult, RegisterService


class TestRegisterResult:
    """Test RegisterResult dataclass."""

    def test_success_result(self):
        """Test creating a successful result."""
        result = RegisterResult(
            success=True,
            session_hash="abc123",
            jobs_registered=3,
            artifacts_registered=5,
            links_created=8,
        )
        assert result.success is True
        assert result.session_hash == "abc123"
        assert result.jobs_registered == 3
        assert result.artifacts_registered == 5
        assert result.links_created == 8
        assert result.error is None

    def test_error_result(self):
        """Test creating an error result."""
        result = RegisterResult(
            success=False,
            error="Something went wrong",
        )
        assert result.success is False
        assert result.error == "Something went wrong"


class TestRegisterService:
    """Test RegisterService class."""

    @pytest.fixture
    def mock_glaas_client(self):
        """Create a mock GLaaS client."""
        client = MagicMock()
        client.health_check.return_value = (True, None)
        client.is_configured.return_value = True
        return client

    @pytest.fixture
    def mock_lineage_collector(self):
        """Create a mock lineage collector."""
        collector = MagicMock()
        return collector

    @pytest.fixture
    def mock_coordinator(self):
        """Create a mock registration coordinator."""
        coordinator = MagicMock()
        return coordinator

    @pytest.fixture
    def mock_session_service(self):
        """Create a mock session service."""
        service = MagicMock()
        return service

    @pytest.fixture
    def service(
        self, mock_glaas_client, mock_lineage_collector, mock_coordinator, mock_session_service
    ):
        """Create a RegisterService with mocked dependencies."""
        return RegisterService(
            glaas_client=mock_glaas_client,
            lineage_collector=mock_lineage_collector,
            coordinator=mock_coordinator,
            session_service=mock_session_service,
        )

    def test_register_lineage_target_dispatches_step_reference(self, service, tmp_path):
        with patch.object(service, "register_step_lineage") as register_step:
            register_step.return_value = RegisterResult(success=True)

            result = service.register_lineage_target(
                target="@4",
                roar_dir=tmp_path / ".roar",
                cwd=tmp_path,
            )

        assert result.success is True
        register_step.assert_called_once_with(
            step_reference="@4",
            roar_dir=tmp_path / ".roar",
            cwd=tmp_path,
            dry_run=False,
            as_blake3=False,
            skip_confirmation=False,
            confirm_callback=None,
        )

    def test_register_lineage_target_dispatches_session_hash(self, service, tmp_path):
        session_hash = "a" * 64
        with (
            patch.object(service, "_resolve_job_target", return_value=None),
            patch.object(service, "_resolve_artifact_hash_target", return_value=None),
            patch.object(service, "register_session_lineage") as register_session,
        ):
            register_session.return_value = RegisterResult(success=True)

            result = service.register_lineage_target(
                target=session_hash,
                roar_dir=tmp_path / ".roar",
                cwd=tmp_path,
            )

        assert result.success is True
        register_session.assert_called_once_with(
            session_hash=session_hash,
            roar_dir=tmp_path / ".roar",
            cwd=tmp_path,
            dry_run=False,
            as_blake3=False,
            skip_confirmation=False,
            confirm_callback=None,
        )

    def test_register_lineage_target_dispatches_session_hash_prefix(self, service, tmp_path):
        session_hash_prefix = "a" * 8
        with (
            patch.object(service, "_resolve_job_target", return_value=None),
            patch.object(service, "_resolve_artifact_hash_target", return_value=None),
            patch.object(service, "register_session_lineage") as register_session,
        ):
            register_session.return_value = RegisterResult(success=True)

            result = service.register_lineage_target(
                target=session_hash_prefix,
                roar_dir=tmp_path / ".roar",
                cwd=tmp_path,
            )

        assert result.success is True
        register_session.assert_called_once_with(
            session_hash=session_hash_prefix,
            roar_dir=tmp_path / ".roar",
            cwd=tmp_path,
            dry_run=False,
            as_blake3=False,
            skip_confirmation=False,
            confirm_callback=None,
        )

    def test_register_lineage_target_dispatches_artifact_path(self, service, tmp_path):
        artifact_path = "metrics.json"
        (tmp_path / artifact_path).write_text("{}\n", encoding="utf-8")
        with patch.object(service, "register_artifact_lineage") as register_artifact:
            register_artifact.return_value = RegisterResult(success=True)

            result = service.register_lineage_target(
                target=artifact_path,
                roar_dir=tmp_path / ".roar",
                cwd=tmp_path,
            )

        assert result.success is True
        register_artifact.assert_called_once_with(
            artifact_path=artifact_path,
            roar_dir=tmp_path / ".roar",
            cwd=tmp_path,
            dry_run=False,
            as_blake3=False,
            skip_confirmation=False,
            confirm_callback=None,
        )

    def test_register_lineage_target_dispatches_job_uid(self, service, tmp_path):
        job_uid = "deadbeef"
        with (
            patch.object(service, "_resolve_job_target", return_value=job_uid),
            patch.object(service, "register_job_lineage") as register_job,
        ):
            register_job.return_value = RegisterResult(success=True)

            result = service.register_lineage_target(
                target=job_uid,
                roar_dir=tmp_path / ".roar",
                cwd=tmp_path,
            )

        assert result.success is True
        register_job.assert_called_once_with(
            job_uid=job_uid,
            roar_dir=tmp_path / ".roar",
            cwd=tmp_path,
            dry_run=False,
            as_blake3=False,
            skip_confirmation=False,
            confirm_callback=None,
        )

    def test_register_lineage_target_dispatches_artifact_hash(self, service, tmp_path):
        artifact_hash = "b" * 64
        with (
            patch.object(service, "_resolve_job_target", return_value=None),
            patch.object(service, "_resolve_artifact_hash_target", return_value=artifact_hash),
            patch.object(service, "register_artifact_hash_lineage") as register_artifact_hash,
        ):
            register_artifact_hash.return_value = RegisterResult(success=True)

            result = service.register_lineage_target(
                target=artifact_hash,
                roar_dir=tmp_path / ".roar",
                cwd=tmp_path,
            )

        assert result.success is True
        register_artifact_hash.assert_called_once_with(
            artifact_hash=artifact_hash,
            roar_dir=tmp_path / ".roar",
            cwd=tmp_path,
            dry_run=False,
            as_blake3=False,
            skip_confirmation=False,
            confirm_callback=None,
        )

    def test_order_jobs_for_registration_puts_parent_before_child(self, service):
        parent = {
            "id": 2,
            "job_uid": "parent-uid",
            "step_number": 1,
            "timestamp": 20.0,
        }
        child = {
            "id": 1,
            "job_uid": "child-uid",
            "parent_job_uid": "parent-uid",
            "step_number": 1,
            "timestamp": 10.0,
        }

        ordered = service._order_jobs_for_registration([child, parent])

        assert [job["job_uid"] for job in ordered] == ["parent-uid", "child-uid"]

    def test_normalize_jobs_for_registration_maps_unresolved_ray_parent_to_submit_job(
        self, service
    ):
        submit_job = {
            "id": 1,
            "job_uid": "local-submit",
            "step_number": 1,
            "timestamp": 10.0,
            "command": "ray job submit --address http://localhost:8265 -- python main.py",
            "job_type": None,
        }
        phase_job = {
            "id": 2,
            "job_uid": "phase-job",
            "parent_job_uid": "remote-driver",
            "step_number": 4,
            "timestamp": 40.0,
            "command": "ray_task:evaluation",
            "job_type": "ray_task",
        }

        normalized = service._normalize_jobs_for_registration([phase_job, submit_job])

        jobs_by_uid = {job["job_uid"]: job for job in normalized}
        assert jobs_by_uid["phase-job"]["parent_job_uid"] == "local-submit"

    def test_normalize_jobs_for_registration_filters_known_ray_noise_jobs(self, service):
        submit_job = {
            "id": 1,
            "job_uid": "local-submit",
            "step_number": 1,
            "timestamp": 10.0,
            "command": "ray job submit --address http://localhost:8265 -- python main.py",
            "job_type": None,
        }
        noise_job = {
            "id": 2,
            "job_uid": "noise-job",
            "parent_job_uid": "local-submit",
            "step_number": 2,
            "timestamp": 20.0,
            "command": "ray_task:unknown",
            "job_type": "ray_task",
        }
        phase_job = {
            "id": 3,
            "job_uid": "phase-job",
            "parent_job_uid": "noise-job",
            "step_number": 3,
            "timestamp": 30.0,
            "command": "ray_task:process_shard",
            "job_type": "ray_task",
        }
        shutdown_job = {
            "id": 4,
            "job_uid": "shutdown-job",
            "parent_job_uid": "local-submit",
            "step_number": 4,
            "timestamp": 40.0,
            "command": "ray_task:shutdown",
            "job_type": "ray_task",
        }

        normalized = service._normalize_jobs_for_registration(
            [submit_job, noise_job, phase_job, shutdown_job]
        )

        assert [job["job_uid"] for job in normalized] == ["local-submit", "phase-job"]
        jobs_by_uid = {job["job_uid"]: job for job in normalized}
        assert jobs_by_uid["phase-job"]["parent_job_uid"] == "local-submit"

    def test_register_artifact_lineage_file_not_found(self, service):
        """Test error when artifact file doesn't exist."""
        result = service.register_artifact_lineage(
            artifact_path="/nonexistent/file.csv",
            roar_dir=Path("/tmp/.roar"),
            cwd=Path("/tmp"),
        )
        assert result.success is False
        assert "not found" in result.error.lower() or "does not exist" in result.error.lower()

    def test_register_artifact_lineage_not_tracked(self, service, tmp_path):
        """Test error when artifact exists but is not tracked in roar."""
        # Create a file that exists but isn't tracked
        artifact_file = tmp_path / "untracked.csv"
        artifact_file.write_text("data")

        # Mock database context to return no artifact
        with patch(
            "roar.services.registration.register_service.create_database_context"
        ) as mock_ctx:
            mock_db = MagicMock()
            mock_db.__enter__ = MagicMock(return_value=mock_db)
            mock_db.__exit__ = MagicMock(return_value=None)
            mock_db.artifacts.get_by_hash.return_value = None
            mock_ctx.return_value = mock_db

            result = service.register_artifact_lineage(
                artifact_path=str(artifact_file),
                roar_dir=tmp_path / ".roar",
                cwd=tmp_path,
            )
            assert result.success is False
            assert "not tracked" in result.error.lower() or "not found" in result.error.lower()

    def test_register_artifact_lineage_no_active_session(self, service, tmp_path):
        """Test error when there is no active session."""
        artifact_file = tmp_path / "file.csv"
        artifact_file.write_text("data")

        # Mock database context
        with patch(
            "roar.services.registration.register_service.create_database_context"
        ) as mock_ctx:
            mock_db = MagicMock()
            mock_db.__enter__ = MagicMock(return_value=mock_db)
            mock_db.__exit__ = MagicMock(return_value=None)
            mock_db.artifacts.get_by_hash.return_value = {"id": "1", "hashes": []}
            mock_db.sessions.get_active.return_value = None  # No active session
            mock_ctx.return_value = mock_db

            result = service.register_artifact_lineage(
                artifact_path=str(artifact_file),
                roar_dir=tmp_path / ".roar",
                cwd=tmp_path,
            )
            assert result.success is False
            assert "session" in result.error.lower()

    def test_register_artifact_lineage_s3_uri_uses_tracked_path(
        self,
        service,
        tmp_path,
        mock_lineage_collector,
    ):
        """S3 artifact registration should resolve by tracked DB path, not local filesystem."""
        artifact_path = "s3://output-bucket/results/run123/final_report.json"

        from roar.core.interfaces.lineage import LineageData

        mock_lineage_collector.collect.return_value = LineageData(
            jobs=[],
            artifacts=[],
            artifact_hashes={"etag123"},
            pipeline={"id": 1},
        )

        with patch(
            "roar.services.registration.register_service.create_database_context"
        ) as mock_ctx:
            mock_db = MagicMock()
            mock_db.__enter__ = MagicMock(return_value=mock_db)
            mock_db.__exit__ = MagicMock(return_value=None)
            mock_db.artifacts.get_by_path.return_value = {
                "id": "artifact-1",
                "hashes": [{"algorithm": "etag", "digest": "etag123"}],
            }
            mock_db.sessions.get_active.return_value = {
                "id": 1,
                "git_commit": "abc123",
                "git_branch": "main",
            }
            mock_ctx.return_value = mock_db

            with patch("roar.services.registration.register_service.GitVCSProvider") as mock_git:
                mock_vcs = MagicMock()
                mock_vcs.get_repo_root.return_value = str(tmp_path)
                mock_vcs.get_remote_url.return_value = "https://github.com/test/repo"
                mock_vcs.get_commit_hash.return_value = "abc123"
                mock_vcs.get_branch.return_value = "main"
                mock_vcs.get_status.return_value = (True, [])
                mock_git.return_value = mock_vcs

                with patch("roar.services.registration.register_service.config_get") as mock_config:
                    mock_config.return_value = False
                    service._session_service.compute_session_hash.return_value = "session-hash-1"

                    result = service.register_artifact_lineage(
                        artifact_path=artifact_path,
                        roar_dir=tmp_path / ".roar",
                        cwd=tmp_path,
                        dry_run=True,
                    )

        assert result.success is True
        mock_db.artifacts.get_by_path.assert_called_once_with(artifact_path)
        mock_db.artifacts.get_by_hash.assert_not_called()
        mock_lineage_collector.collect.assert_called_once_with(["etag123"], tmp_path / ".roar")

    def test_register_artifact_lineage_dry_run(self, service, tmp_path, mock_lineage_collector):
        """Test dry-run mode returns counts without calling API."""
        artifact_file = tmp_path / "file.csv"
        artifact_file.write_text("data")

        # Mock LineageData
        from roar.core.interfaces.lineage import LineageData

        mock_lineage = LineageData(
            jobs=[{"id": 1, "job_uid": "job1"}, {"id": 2, "job_uid": "job2"}],
            artifacts=[{"id": "a1"}, {"id": "a2"}, {"id": "a3"}],
            artifact_hashes={"hash1", "hash2", "hash3"},
            pipeline={"id": 1},
        )
        mock_lineage_collector.collect.return_value = mock_lineage

        # Mock database context
        with patch(
            "roar.services.registration.register_service.create_database_context"
        ) as mock_ctx:
            mock_db = MagicMock()
            mock_db.__enter__ = MagicMock(return_value=mock_db)
            mock_db.__exit__ = MagicMock(return_value=None)
            mock_db.artifacts.get_by_hash.return_value = {
                "id": "1",
                "hashes": [{"algorithm": "blake3", "digest": "abc123"}],
            }
            mock_db.sessions.get_active.return_value = {
                "id": 1,
                "git_commit": "abc",
                "git_branch": "main",
            }
            mock_ctx.return_value = mock_db

            # Mock git context retrieval
            with patch("roar.services.registration.register_service.GitVCSProvider") as mock_git:
                mock_vcs = MagicMock()
                mock_vcs.get_repo_root.return_value = str(tmp_path)
                mock_vcs.get_remote_url.return_value = "https://github.com/test/repo"
                mock_vcs.get_commit_hash.return_value = "abc123"
                mock_vcs.get_branch.return_value = "main"
                mock_vcs.get_status.return_value = (True, [])  # Clean repo
                mock_git.return_value = mock_vcs

                # Mock config_get to return False for tagging (skip dirty check)
                with patch("roar.services.registration.register_service.config_get") as mock_config:
                    mock_config.return_value = False

                    result = service.register_artifact_lineage(
                        artifact_path=str(artifact_file),
                        roar_dir=tmp_path / ".roar",
                        cwd=tmp_path,
                        dry_run=True,
                    )

        assert result.success is True
        assert result.jobs_registered == 2
        assert result.artifacts_registered == 3
        # In dry-run mode, no actual API calls should be made
        service._coordinator.register_lineage.assert_not_called()

    def test_register_artifact_lineage_dry_run_filters_known_ray_noise_jobs(
        self,
        service,
        tmp_path,
        mock_lineage_collector,
        mock_session_service,
    ):
        """Dry-run counts should exclude known internal Ray/bootstrap jobs."""
        artifact_file = tmp_path / "file.csv"
        artifact_file.write_text("data")

        from roar.core.interfaces.lineage import LineageData

        mock_lineage = LineageData(
            jobs=[
                {
                    "id": 1,
                    "job_uid": "local-submit",
                    "step_number": 1,
                    "timestamp": 10.0,
                    "command": "ray job submit --address http://localhost:8265 -- python main.py",
                    "job_type": None,
                    "_outputs": [{"artifact_id": "driver-output"}],
                },
                {
                    "id": 2,
                    "job_uid": "noise-job",
                    "parent_job_uid": "local-submit",
                    "step_number": 2,
                    "timestamp": 20.0,
                    "command": "ray_task:unknown",
                    "job_type": "ray_task",
                    "_outputs": [{"artifact_id": "noise-output"}],
                },
                {
                    "id": 3,
                    "job_uid": "phase-job",
                    "parent_job_uid": "noise-job",
                    "step_number": 3,
                    "timestamp": 30.0,
                    "command": "ray_task:process_shard",
                    "job_type": "ray_task",
                    "_inputs": [{"artifact_id": "driver-output"}],
                    "_outputs": [{"artifact_id": "task-output"}],
                },
            ],
            artifacts=[{"id": "a1"}],
            artifact_hashes={"hash1"},
            pipeline={"id": 1},
        )
        mock_lineage_collector.collect.return_value = mock_lineage
        mock_session_service.compute_session_hash.return_value = "session-hash-1"

        with patch(
            "roar.services.registration.register_service.create_database_context"
        ) as mock_ctx:
            mock_db = MagicMock()
            mock_db.__enter__ = MagicMock(return_value=mock_db)
            mock_db.__exit__ = MagicMock(return_value=None)
            mock_db.artifacts.get_by_hash.return_value = {
                "id": "1",
                "hashes": [{"algorithm": "blake3", "digest": "abc123"}],
            }
            mock_db.sessions.get_active.return_value = {
                "id": 1,
                "git_commit": "abc",
                "git_branch": "main",
            }
            mock_ctx.return_value = mock_db

            with patch("roar.services.registration.register_service.GitVCSProvider") as mock_git:
                mock_vcs = MagicMock()
                mock_vcs.get_repo_root.return_value = str(tmp_path)
                mock_vcs.get_remote_url.return_value = "https://github.com/test/repo"
                mock_vcs.get_commit_hash.return_value = "abc123"
                mock_vcs.get_branch.return_value = "main"
                mock_vcs.get_status.return_value = (True, [])
                mock_git.return_value = mock_vcs

                with patch("roar.services.registration.register_service.config_get") as mock_config:
                    mock_config.return_value = False

                    result = service.register_artifact_lineage(
                        artifact_path=str(artifact_file),
                        roar_dir=tmp_path / ".roar",
                        cwd=tmp_path,
                        dry_run=True,
                    )

        assert result.success is True
        assert result.jobs_registered == 2
        assert result.links_created == 3
        service._coordinator.register_lineage.assert_not_called()

    def test_register_artifact_lineage_glaas_health_check_fails(
        self, service, tmp_path, mock_glaas_client, mock_lineage_collector
    ):
        """Test error when GLaaS health check fails."""
        artifact_file = tmp_path / "file.csv"
        artifact_file.write_text("data")

        # Make health check fail
        from roar.core.exceptions import GlaasConnectionError

        mock_glaas_client.health_check.side_effect = GlaasConnectionError("Connection refused")

        # Mock LineageData
        from roar.core.interfaces.lineage import LineageData

        mock_lineage = LineageData(
            jobs=[{"id": 1, "job_uid": "job1"}],
            artifacts=[{"id": "a1"}],
            artifact_hashes={"hash1"},
            pipeline={"id": 1},
        )
        mock_lineage_collector.collect.return_value = mock_lineage

        # Mock database context
        with patch(
            "roar.services.registration.register_service.create_database_context"
        ) as mock_ctx:
            mock_db = MagicMock()
            mock_db.__enter__ = MagicMock(return_value=mock_db)
            mock_db.__exit__ = MagicMock(return_value=None)
            mock_db.artifacts.get_by_hash.return_value = {
                "id": "1",
                "hashes": [{"algorithm": "blake3", "digest": "abc123"}],
            }
            mock_db.sessions.get_active.return_value = {
                "id": 1,
                "git_commit": "abc",
                "git_branch": "main",
            }
            mock_ctx.return_value = mock_db

            # Mock git context retrieval
            with patch("roar.services.registration.register_service.GitVCSProvider") as mock_git:
                mock_vcs = MagicMock()
                mock_vcs.get_repo_root.return_value = str(tmp_path)
                mock_vcs.get_remote_url.return_value = "https://github.com/test/repo"
                mock_vcs.get_commit_hash.return_value = "abc123"
                mock_vcs.get_branch.return_value = "main"
                mock_vcs.get_status.return_value = (True, [])  # Clean repo
                mock_git.return_value = mock_vcs

                # Mock config_get to return False for tagging (skip dirty check)
                with patch("roar.services.registration.register_service.config_get") as mock_config:
                    mock_config.return_value = False

                    result = service.register_artifact_lineage(
                        artifact_path=str(artifact_file),
                        roar_dir=tmp_path / ".roar",
                        cwd=tmp_path,
                        dry_run=False,
                    )

        assert result.success is False
        assert "health" in result.error.lower() or "connection" in result.error.lower()

    def test_register_artifact_lineage_glaas_not_configured(self, tmp_path):
        """Test error when GLaaS is not configured."""
        artifact_file = tmp_path / "file.csv"
        artifact_file.write_text("data")

        mock_client = MagicMock()
        mock_client.is_configured.return_value = False

        service = RegisterService(glaas_client=mock_client)

        # Mock database context
        with patch(
            "roar.services.registration.register_service.create_database_context"
        ) as mock_ctx:
            mock_db = MagicMock()
            mock_db.__enter__ = MagicMock(return_value=mock_db)
            mock_db.__exit__ = MagicMock(return_value=None)
            mock_db.artifacts.get_by_hash.return_value = {
                "id": "1",
                "hashes": [{"algorithm": "blake3", "digest": "abc123"}],
            }
            mock_db.sessions.get_active.return_value = {
                "id": 1,
                "git_commit": "abc",
                "git_branch": "main",
            }
            mock_ctx.return_value = mock_db

            # Mock git context retrieval
            with patch("roar.services.registration.register_service.GitVCSProvider") as mock_git:
                mock_vcs = MagicMock()
                mock_vcs.get_repo_root.return_value = str(tmp_path)
                mock_vcs.get_remote_url.return_value = "https://github.com/test/repo"
                mock_vcs.get_commit_hash.return_value = "abc123"
                mock_vcs.get_branch.return_value = "main"
                mock_vcs.get_status.return_value = (True, [])  # Clean repo
                mock_git.return_value = mock_vcs

                # Mock config_get to return False for tagging (skip dirty check)
                with patch("roar.services.registration.register_service.config_get") as mock_config:
                    mock_config.return_value = False

                    result = service.register_artifact_lineage(
                        artifact_path=str(artifact_file),
                        roar_dir=tmp_path / ".roar",
                        cwd=tmp_path,
                    )

        assert result.success is False
        assert "not configured" in result.error.lower() or "glaas" in result.error.lower()

    def test_register_result_includes_artifact_hash(self):
        """Test that RegisterResult includes artifact_hash field."""
        result = RegisterResult(
            success=True,
            session_hash="session123",
            artifact_hash="artifact456",
            jobs_registered=2,
            artifacts_registered=3,
            links_created=5,
        )
        assert result.artifact_hash == "artifact456"

    def test_register_artifact_lineage_dirty_repo_fails(
        self, service, tmp_path, mock_lineage_collector
    ):
        """Test that registration fails with uncommitted changes when tagging is enabled."""
        artifact_file = tmp_path / "file.csv"
        artifact_file.write_text("data")

        # Mock database context
        with patch(
            "roar.services.registration.register_service.create_database_context"
        ) as mock_ctx:
            mock_db = MagicMock()
            mock_db.__enter__ = MagicMock(return_value=mock_db)
            mock_db.__exit__ = MagicMock(return_value=None)
            mock_db.artifacts.get_by_hash.return_value = {
                "id": "1",
                "hashes": [{"algorithm": "blake3", "digest": "abc123"}],
            }
            mock_db.sessions.get_active.return_value = {
                "id": 1,
                "git_commit": "abc",
                "git_branch": "main",
            }
            mock_ctx.return_value = mock_db

            # Mock git context and dirty-repo policy
            with (
                patch("roar.services.registration.register_service.GitVCSProvider") as mock_git,
                patch(
                    "roar.services.registration.register_service.ensure_clean_publish_repo",
                    side_effect=ValueError(
                        "Cannot register with uncommitted changes. Commit your changes first."
                    ),
                ),
            ):
                mock_vcs = MagicMock()
                mock_vcs.get_repo_root.return_value = str(tmp_path)
                mock_vcs.get_remote_url.return_value = "https://github.com/test/repo"
                mock_vcs.get_commit_hash.return_value = "abc123def456"
                mock_vcs.get_branch.return_value = "main"
                mock_git.return_value = mock_vcs

                # Mock config to enable tagging
                with patch("roar.services.registration.register_service.config_get") as mock_config:
                    mock_config.return_value = True  # tagging enabled

                    result = service.register_artifact_lineage(
                        artifact_path=str(artifact_file),
                        roar_dir=tmp_path / ".roar",
                        cwd=tmp_path,
                    )

        assert result.success is False
        assert "uncommitted" in result.error.lower()

    def test_register_artifact_lineage_creates_git_tag(
        self, service, tmp_path, mock_lineage_collector, mock_session_service, mock_coordinator
    ):
        """Test that successful registration creates a git tag."""
        artifact_file = tmp_path / "file.csv"
        artifact_file.write_text("data")

        # Mock LineageData
        from roar.core.interfaces.lineage import LineageData

        mock_lineage = LineageData(
            jobs=[{"id": 1, "job_uid": "job1"}],
            artifacts=[{"id": "a1"}],
            artifact_hashes={"hash1"},
            pipeline={"id": 1},
        )
        mock_lineage_collector.collect.return_value = mock_lineage

        # Mock session registration success
        mock_session_result = MagicMock()
        mock_session_result.success = True
        mock_session_service.register.return_value = mock_session_result
        mock_session_service.compute_session_hash.return_value = "session_hash_123"

        # Mock coordinator batch result
        from roar.core.interfaces.registration import BatchRegistrationResult

        mock_batch_result = BatchRegistrationResult(
            session_registered=True,
            jobs_created=1,
            jobs_failed=0,
            artifacts_registered=1,
            artifacts_failed=0,
            links_created=2,
            links_failed=0,
            errors=[],
        )
        mock_coordinator.register_lineage.return_value = mock_batch_result

        # Mock database context
        with patch(
            "roar.services.registration.register_service.create_database_context"
        ) as mock_ctx:
            mock_db = MagicMock()
            mock_db.__enter__ = MagicMock(return_value=mock_db)
            mock_db.__exit__ = MagicMock(return_value=None)
            mock_db.artifacts.get_by_hash.return_value = {
                "id": "1",
                "hashes": [{"algorithm": "blake3", "digest": "abc123"}],
            }
            mock_db.sessions.get_active.return_value = {
                "id": 1,
                "git_commit": "abc",
                "git_branch": "main",
            }
            mock_ctx.return_value = mock_db

            # Mock git context and shared publish git policy
            with (
                patch("roar.services.registration.register_service.GitVCSProvider") as mock_git,
                patch(
                    "roar.services.registration.register_service.ensure_clean_publish_repo",
                    return_value=MagicMock(),
                ),
                patch(
                    "roar.services.registration.register_service.create_publish_git_tag",
                    return_value=(True, None),
                ) as mock_create_tag,
            ):
                mock_vcs = MagicMock()
                mock_vcs.get_repo_root.return_value = str(tmp_path)
                mock_vcs.get_remote_url.return_value = "https://github.com/test/repo"
                mock_vcs.get_commit_hash.return_value = "abc123def456"
                mock_vcs.get_branch.return_value = "main"
                mock_git.return_value = mock_vcs

                # Mock config to enable tagging
                with patch("roar.services.registration.register_service.config_get") as mock_config:

                    def config_side_effect(key):
                        if key == "registration.tagging.enabled":
                            return True
                        elif key == "registration.omit":
                            return {"enabled": False}  # Disable omit filter
                        return None

                    mock_config.side_effect = config_side_effect

                    result = service.register_artifact_lineage(
                        artifact_path=str(artifact_file),
                        roar_dir=tmp_path / ".roar",
                        cwd=tmp_path,
                    )

        assert result.success is True
        mock_create_tag.assert_called_once_with(tmp_path, "roar/abc123de")

    def test_register_artifact_lineage_tagging_disabled_skips_dirty_check(
        self, service, tmp_path, mock_lineage_collector
    ):
        """Test that dirty repo check is skipped when tagging is disabled."""
        artifact_file = tmp_path / "file.csv"
        artifact_file.write_text("data")

        # Mock database context
        with patch(
            "roar.services.registration.register_service.create_database_context"
        ) as mock_ctx:
            mock_db = MagicMock()
            mock_db.__enter__ = MagicMock(return_value=mock_db)
            mock_db.__exit__ = MagicMock(return_value=None)
            mock_db.artifacts.get_by_hash.return_value = {
                "id": "1",
                "hashes": [{"algorithm": "blake3", "digest": "abc123"}],
            }
            mock_db.sessions.get_active.return_value = {
                "id": 1,
                "git_commit": "abc",
                "git_branch": "main",
            }
            mock_ctx.return_value = mock_db

            # Mock git context
            with patch("roar.services.registration.register_service.GitVCSProvider") as mock_git:
                mock_vcs = MagicMock()
                mock_vcs.get_repo_root.return_value = str(tmp_path)
                mock_vcs.get_remote_url.return_value = "https://github.com/test/repo"
                mock_vcs.get_commit_hash.return_value = "abc123def456"
                mock_vcs.get_branch.return_value = "main"
                # Note: get_status not called because tagging is disabled
                mock_git.return_value = mock_vcs

                # Mock config to disable tagging
                with patch("roar.services.registration.register_service.config_get") as mock_config:
                    mock_config.return_value = False  # tagging disabled

                    # Dry run to avoid full registration flow
                    result = service.register_artifact_lineage(
                        artifact_path=str(artifact_file),
                        roar_dir=tmp_path / ".roar",
                        cwd=tmp_path,
                        dry_run=True,
                    )

        # Should succeed in dry-run mode (dirty check was skipped)
        assert result.success is True
        # get_status should not be called when tagging is disabled
        mock_vcs.get_status.assert_not_called()

    def test_get_git_context_falls_back_to_local_repo_uri_when_remote_missing(self, tmp_path):
        """If no origin remote exists, use a local file:// repo URI and warn."""
        mock_logger = MagicMock()
        service = RegisterService(logger=mock_logger)

        with patch("roar.services.registration.register_service.GitVCSProvider") as mock_git:
            mock_vcs = MagicMock()
            mock_vcs.get_repo_root.return_value = str(tmp_path)
            mock_vcs.get_remote_url.return_value = None
            mock_vcs.get_commit_hash.return_value = "abc123def456"
            mock_vcs.get_branch.return_value = "main"
            mock_git.return_value = mock_vcs

            git_context = service._get_git_context(tmp_path)

        assert git_context.repo == tmp_path.resolve().as_uri()
        assert git_context.commit == "abc123def456"
        assert git_context.branch == "main"
        assert mock_logger.warning.call_count >= 1
        assert any(
            "No git remote configured" in str(call.args[0])
            for call in mock_logger.warning.call_args_list
        )

    def test_register_artifact_lineage_uses_local_repo_uri_when_remote_missing(
        self,
        service,
        tmp_path,
        mock_lineage_collector,
        mock_session_service,
        mock_coordinator,
    ):
        """Registration should not fail solely because origin remote is missing."""
        artifact_file = tmp_path / "file.csv"
        artifact_file.write_text("data")

        # Mock LineageData
        from roar.core.interfaces.lineage import LineageData

        mock_lineage = LineageData(
            jobs=[{"id": 1, "job_uid": "job1"}],
            artifacts=[{"id": "a1"}],
            artifact_hashes={"hash1"},
            pipeline={"id": 1},
        )
        mock_lineage_collector.collect.return_value = mock_lineage

        # Mock session registration success
        mock_session_result = MagicMock()
        mock_session_result.success = True
        mock_session_service.register.return_value = mock_session_result
        mock_session_service.compute_session_hash.return_value = "session_hash_123"

        # Mock coordinator batch result
        from roar.core.interfaces.registration import BatchRegistrationResult

        mock_batch_result = BatchRegistrationResult(
            session_registered=True,
            jobs_created=1,
            jobs_failed=0,
            artifacts_registered=1,
            artifacts_failed=0,
            links_created=1,
            links_failed=0,
            errors=[],
        )
        mock_coordinator.register_lineage.return_value = mock_batch_result

        # Mock database context
        with patch(
            "roar.services.registration.register_service.create_database_context"
        ) as mock_ctx:
            mock_db = MagicMock()
            mock_db.__enter__ = MagicMock(return_value=mock_db)
            mock_db.__exit__ = MagicMock(return_value=None)
            mock_db.artifacts.get_by_hash.return_value = {
                "id": "1",
                "hashes": [{"algorithm": "blake3", "digest": "abc123"}],
            }
            mock_db.sessions.get_active.return_value = {
                "id": 1,
                "git_commit": "abc",
                "git_branch": "main",
            }
            mock_ctx.return_value = mock_db

            # Mock git context retrieval with missing remote
            with patch("roar.services.registration.register_service.GitVCSProvider") as mock_git:
                mock_vcs = MagicMock()
                mock_vcs.get_repo_root.return_value = str(tmp_path)
                mock_vcs.get_remote_url.return_value = None
                mock_vcs.get_commit_hash.return_value = "abc123def456"
                mock_vcs.get_branch.return_value = "main"
                mock_vcs.get_status.return_value = (True, [])  # Clean repo
                mock_git.return_value = mock_vcs

                # Disable tagging + omit filtering for focused behavior
                with patch("roar.services.registration.register_service.config_get") as mock_config:

                    def config_side_effect(key):
                        if key == "registration.tagging.enabled":
                            return False
                        if key == "registration.omit":
                            return {"enabled": False}
                        return None

                    mock_config.side_effect = config_side_effect

                    result = service.register_artifact_lineage(
                        artifact_path=str(artifact_file),
                        roar_dir=tmp_path / ".roar",
                        cwd=tmp_path,
                    )

        assert result.success is True
        assert mock_session_service.register.called
        registered_git_context = mock_session_service.register.call_args.args[1]
        assert registered_git_context.repo == tmp_path.resolve().as_uri()

    def test_prepare_artifacts_preserves_etag_algorithm_for_etag_only_artifact(self):
        service = RegisterService()
        etag_digest = "1234567890abcdef1234567890abcdef"
        artifacts = [
            {
                "hash": etag_digest,
                "hashes": [{"algorithm": "etag", "digest": etag_digest}],
                "size": 42,
                "source_type": "s3",
            }
        ]

        prepared = service._prepare_artifacts(artifacts, session_hash="session-1")

        assert len(prepared) == 1
        assert prepared[0]["hashes"] == [{"algorithm": "etag", "digest": etag_digest}]
        assert prepared[0]["size"] == 42
        assert prepared[0]["source_type"] == "s3"

    def test_prepare_artifacts_puts_blake3_first_when_multiple_hashes_exist(self):
        service = RegisterService()
        etag_digest = "etag-digest-1"
        blake3_digest = "b" * 64
        artifacts = [
            {
                "hash": etag_digest,
                "hashes": [
                    {"algorithm": "etag", "digest": etag_digest},
                    {"algorithm": "blake3", "digest": blake3_digest},
                ],
                "size": 100,
                "source_type": "s3",
            }
        ]

        prepared = service._prepare_artifacts(artifacts, session_hash="session-1")

        assert len(prepared) == 1
        assert prepared[0]["hashes"] == [
            {"algorithm": "blake3", "digest": blake3_digest},
            {"algorithm": "etag", "digest": etag_digest},
        ]

    def test_prepare_artifacts_keeps_blake3_for_blake3_only_artifact(self):
        service = RegisterService()
        blake3_digest = "a" * 64
        artifacts = [
            {
                "hash": blake3_digest,
                "hashes": [{"algorithm": "blake3", "digest": blake3_digest}],
                "size": 9,
                "source_type": "local",
            }
        ]

        prepared = service._prepare_artifacts(artifacts, session_hash="session-1")

        assert len(prepared) == 1
        assert prepared[0]["hashes"] == [{"algorithm": "blake3", "digest": blake3_digest}]

    def test_prepare_artifacts_skips_composite_artifacts(self):
        service = RegisterService()
        composite_digest = "c" * 64
        primitive_digest = "a" * 64
        artifacts = [
            {
                "kind": "composite",
                "hashes": [{"algorithm": "composite-blake3", "digest": composite_digest}],
                "size": 11,
                "source_type": "local",
            },
            {
                "hashes": [{"algorithm": "blake3", "digest": primitive_digest}],
                "size": 9,
                "source_type": "local",
            },
        ]

        prepared = service._prepare_artifacts(artifacts, session_hash="session-1")

        assert prepared == [
            {
                "hashes": [{"algorithm": "blake3", "digest": primitive_digest}],
                "size": 9,
                "source_type": None,
                "session_hash": "session-1",
            }
        ]

    def test_prepare_artifacts_skips_artifacts_without_usable_hash(self):
        service = RegisterService()
        artifacts = [
            {
                "hashes": [
                    {"algorithm": "etag", "digest": ""},
                    {"algorithm": "blake3", "digest": None},
                    {"algorithm": "", "digest": "abc"},
                ],
                "size": 1,
                "source_type": "s3",
            }
        ]

        prepared = service._prepare_artifacts(artifacts, session_hash="session-1")

        assert prepared == []

    def test_build_lineage_membership_index_payload_rebuilds_full_component_bloom(self):
        service = RegisterService()

        payload = service._build_lineage_membership_index_payload(
            membership_index={
                "total_components": 2,
                "stored_components": 2,
                "bloom_filter_base64": "AQIDBA==",
                "bloom_bits": 2048,
                "bloom_hashes": 12,
                "bloom_version": 1,
            },
            component_count_total=2,
            components=[
                {
                    "relative_path": "part-000.json",
                    "leaf_kind": "file",
                    "component_algorithm": "blake3",
                    "component_digest": "d" * 64,
                    "component_size": 5,
                    "component_type": "application/json",
                },
                {
                    "relative_path": "part-001.json",
                    "leaf_kind": "file",
                    "component_algorithm": "blake3",
                    "component_digest": "e" * 64,
                    "component_size": 8,
                    "component_type": "application/json",
                },
            ],
        )

        assert payload["total_components"] == 2
        assert payload["stored_components"] == 2
        assert payload["bloom_filter_base64"] != "AQIDBA=="
        assert payload["bloom_bits"] > 0
        assert payload["bloom_hashes"] > 0
        assert payload["bloom_version"] == 1

    def test_register_collected_lineage_preregisters_composites_before_batch_registration(
        self,
        service,
        tmp_path,
        mock_glaas_client,
        mock_coordinator,
        mock_session_service,
    ):
        from roar.core.interfaces.lineage import LineageData
        from roar.core.interfaces.registration import (
            BatchRegistrationResult,
            GitContext,
            SessionRegistrationResult,
        )

        primitive_digest = "a" * 64
        composite_digest = "c" * 64
        composite_root = tmp_path / "exports" / "bundle"

        lineage = LineageData(
            jobs=[
                {
                    "id": 1,
                    "job_uid": "job-1",
                    "step_number": 1,
                    "timestamp": 1.0,
                }
            ],
            artifacts=[
                {
                    "id": "primitive-1",
                    "hashes": [{"algorithm": "blake3", "digest": primitive_digest}],
                    "size": 7,
                    "source_type": "local",
                },
                {
                    "id": "composite-1",
                    "kind": "composite",
                    "first_seen_path": str(composite_root),
                    "hashes": [{"algorithm": "composite-blake3", "digest": composite_digest}],
                    "size": 13,
                    "component_count": 2,
                    "source_type": "local",
                },
            ],
            artifact_hashes={primitive_digest, composite_digest},
            pipeline={"id": 1},
        )

        mock_session_service.compute_session_hash.return_value = "session-hash-123"
        mock_session_service.register.return_value = SessionRegistrationResult(
            success=True,
            session_hash="session-hash-123",
            session_url="https://glaas.example/dag/session-hash-123",
        )
        mock_glaas_client.register_composite_artifact.return_value = (
            {"artifact_id": "srv-comp-1", "created": True},
            None,
        )
        mock_coordinator.register_lineage.return_value = BatchRegistrationResult(
            session_registered=True,
            jobs_created=1,
            jobs_failed=0,
            artifacts_registered=1,
            artifacts_failed=0,
            links_created=0,
            links_failed=0,
            errors=[],
        )

        with (
            patch(
                "roar.services.registration.register_service.create_database_context"
            ) as mock_ctx,
            patch("roar.services.registration.register_service.config_get", return_value=False),
            patch.object(
                service,
                "_get_git_context",
                return_value=GitContext(
                    repo="https://github.com/test/repo",
                    commit="abc123def456",
                    branch="main",
                ),
            ),
        ):
            mock_db = MagicMock()
            mock_db.__enter__ = MagicMock(return_value=mock_db)
            mock_db.__exit__ = MagicMock(return_value=None)
            mock_db.composites = MagicMock()
            mock_db.composites.get_components.return_value = [
                {
                    "relative_path": "part-000.json",
                    "leaf_kind": "file",
                    "component_algorithm": "blake3",
                    "component_digest": "d" * 64,
                    "component_size": 5,
                    "component_type": "application/json",
                },
                {
                    "relative_path": "part-001.json",
                    "leaf_kind": "file",
                    "component_algorithm": "blake3",
                    "component_digest": "e" * 64,
                    "component_size": 8,
                    "component_type": "application/json",
                },
            ]
            mock_db.composites.get_membership_index.return_value = None
            mock_ctx.return_value = mock_db

            result = service._register_collected_lineage(
                lineage=lineage,
                roar_dir=tmp_path / ".roar",
                cwd=tmp_path,
                session_id=1,
                artifact_hash=primitive_digest,
                dry_run=False,
                as_blake3=False,
                skip_confirmation=False,
                confirm_callback=None,
            )

        assert result.success is True
        assert result.artifacts_registered == 2

        composite_payload = mock_glaas_client.register_composite_artifact.call_args.args[0]
        assert composite_payload["hash"] == composite_digest
        assert composite_payload["source_type"] is None
        assert composite_payload["component_count_total"] == 2
        assert len(composite_payload["components"]) == 2
        assert composite_payload["membership_index"]["total_components"] == 2
        assert composite_payload["membership_index"]["stored_components"] == 2
        assert composite_payload["membership_index"]["bloom_filter_base64"]
        assert composite_payload["membership_index"]["bloom_bits"] > 0
        assert composite_payload["membership_index"]["bloom_hashes"] > 0
        assert composite_payload["membership_index"]["bloom_version"] == 1

        coordinator_artifacts = mock_coordinator.register_lineage.call_args.kwargs["artifacts"]
        assert coordinator_artifacts == [
            {
                "hashes": [{"algorithm": "blake3", "digest": primitive_digest}],
                "size": 7,
                "source_type": None,
                "session_hash": "session-hash-123",
            }
        ]
