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

        from roar.core.interfaces.upload import LineageData

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
        from roar.core.interfaces.upload import LineageData

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
        from roar.core.interfaces.upload import LineageData

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

            # Mock git context and status (dirty repo)
            with patch("roar.services.registration.register_service.GitVCSProvider") as mock_git:
                mock_vcs = MagicMock()
                mock_vcs.get_repo_root.return_value = str(tmp_path)
                mock_vcs.get_remote_url.return_value = "https://github.com/test/repo"
                mock_vcs.get_commit_hash.return_value = "abc123def456"
                mock_vcs.get_branch.return_value = "main"
                mock_vcs.get_status.return_value = (False, ["M file.txt"])  # Dirty repo
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
        from roar.core.interfaces.upload import LineageData

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

            # Mock git context (clean repo)
            with patch("roar.services.registration.register_service.GitVCSProvider") as mock_git:
                mock_vcs = MagicMock()
                mock_vcs.get_repo_root.return_value = str(tmp_path)
                mock_vcs.get_remote_url.return_value = "https://github.com/test/repo"
                mock_vcs.get_commit_hash.return_value = "abc123def456"
                mock_vcs.get_branch.return_value = "main"
                mock_vcs.get_status.return_value = (True, [])  # Clean repo
                mock_vcs.create_tag.return_value = (True, None)  # Tag creation succeeds
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
        # Verify create_tag was called with expected tag name
        mock_vcs.create_tag.assert_called_once()
        call_args = mock_vcs.create_tag.call_args
        assert call_args[0][1] == "roar/abc123de"  # roar/{commit[:8]}

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
        from roar.core.interfaces.upload import LineageData

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
