"""
Unit tests for the 'roar show' CLI command with artifact lookups.

Tests the CLI behavior with mocked dependencies:
- Show artifact by hash (full or prefix)
- Show artifact by file path
- Reference type disambiguation
- Edge cases
"""

import sys
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

import roar.cli.commands.show  # noqa: F401 - ensure module is in sys.modules
from roar.cli.commands.show import show

show_module = sys.modules["roar.cli.commands.show"]


class TestShowArtifactByHash:
    """Tests for hash-based artifact lookup."""

    @pytest.fixture
    def runner(self):
        """Create a Click CLI test runner."""
        return CliRunner()

    @pytest.fixture
    def mock_ctx(self, tmp_path):
        """Create a mock RoarContext."""
        ctx = MagicMock()
        ctx.roar_dir = tmp_path / ".roar"
        ctx.roar_dir.mkdir()
        ctx.cwd = tmp_path
        ctx.is_initialized = True
        return ctx

    def test_show_artifact_by_full_hash(self, runner, mock_ctx):
        """Full 64-char hash displays artifact information."""
        full_hash = "a1b2c3d4e5f67890" * 4  # 64 chars

        with patch.object(show_module, "create_database_context") as mock_db:
            db_ctx = MagicMock()
            mock_db.return_value.__enter__.return_value = db_ctx

            # No job with this UID
            db_ctx.jobs.get_by_uid.return_value = None

            # Artifact found
            db_ctx.artifacts.get_by_hash.return_value = {
                "id": "artifact-123",
                "size": 1024,
                "first_seen_at": 1700000000.0,
                "first_seen_path": "/data/model.pkl",
                "hashes": [
                    {"algorithm": "blake3", "digest": full_hash},
                    {"algorithm": "sha256", "digest": "sha256hash" * 6},
                ],
            }
            db_ctx.artifacts.get_locations.return_value = [{"path": "/data/model.pkl"}]
            db_ctx.artifacts.get_jobs.return_value = {"produced_by": [], "consumed_by": []}

            result = runner.invoke(show, [full_hash], obj=mock_ctx)

        assert result.exit_code == 0, f"Exit code was {result.exit_code}: {result.output}"
        assert "Artifact:" in result.output
        assert "artifact-123" in result.output

    def test_show_artifact_by_hash_prefix(self, runner, mock_ctx):
        """16+ char hash prefix works for artifact lookup."""
        hash_prefix = "a1b2c3d4e5f67890"  # 16 chars

        with patch.object(show_module, "create_database_context") as mock_db:
            db_ctx = MagicMock()
            mock_db.return_value.__enter__.return_value = db_ctx

            # No job with this UID
            db_ctx.jobs.get_by_uid.return_value = None

            # Artifact found
            db_ctx.artifacts.get_by_hash.return_value = {
                "id": "artifact-456",
                "size": 2048,
                "first_seen_at": 1700000000.0,
                "first_seen_path": None,
                "hashes": [{"algorithm": "blake3", "digest": hash_prefix + "x" * 48}],
            }
            db_ctx.artifacts.get_locations.return_value = []
            db_ctx.artifacts.get_jobs.return_value = {"produced_by": [], "consumed_by": []}

            result = runner.invoke(show, [hash_prefix], obj=mock_ctx)

        assert result.exit_code == 0
        assert "Artifact:" in result.output

    def test_show_artifact_hash_not_found(self, runner, mock_ctx):
        """Shows 'not found' message for unknown hash."""
        unknown_hash = "deadbeef12345678"  # 16 chars, not found

        with patch.object(show_module, "create_database_context") as mock_db:
            db_ctx = MagicMock()
            mock_db.return_value.__enter__.return_value = db_ctx

            db_ctx.jobs.get_by_uid.return_value = None
            db_ctx.artifacts.get_by_hash.return_value = None

            result = runner.invoke(show, [unknown_hash], obj=mock_ctx)

        assert result.exit_code == 0
        assert "Not found" in result.output or "not found" in result.output.lower()

    def test_show_artifact_displays_all_hashes(self, runner, mock_ctx):
        """Artifact display shows all hash algorithms."""
        full_hash = "a1b2c3d4e5f67890" * 4

        with patch.object(show_module, "create_database_context") as mock_db:
            db_ctx = MagicMock()
            mock_db.return_value.__enter__.return_value = db_ctx

            db_ctx.jobs.get_by_uid.return_value = None
            db_ctx.artifacts.get_by_hash.return_value = {
                "id": "artifact-789",
                "size": 4096,
                "first_seen_at": 1700000000.0,
                "first_seen_path": None,
                "hashes": [
                    {"algorithm": "blake3", "digest": "blake3hash" + "0" * 54},
                    {"algorithm": "sha256", "digest": "sha256hash" + "0" * 54},
                ],
            }
            db_ctx.artifacts.get_locations.return_value = []
            db_ctx.artifacts.get_jobs.return_value = {"produced_by": [], "consumed_by": []}

            result = runner.invoke(show, [full_hash], obj=mock_ctx)

        assert result.exit_code == 0
        assert "blake3" in result.output.lower()
        assert "sha256" in result.output.lower()

    def test_show_artifact_displays_locations(self, runner, mock_ctx):
        """Artifact display shows all file paths where artifact was seen."""
        full_hash = "a1b2c3d4e5f67890" * 4

        with patch.object(show_module, "create_database_context") as mock_db:
            db_ctx = MagicMock()
            mock_db.return_value.__enter__.return_value = db_ctx

            db_ctx.jobs.get_by_uid.return_value = None
            db_ctx.artifacts.get_by_hash.return_value = {
                "id": "artifact-loc",
                "size": 8192,
                "first_seen_at": 1700000000.0,
                "first_seen_path": "/original/path.pkl",
                "hashes": [{"algorithm": "blake3", "digest": full_hash}],
            }
            db_ctx.artifacts.get_locations.return_value = [
                {"path": "/original/path.pkl"},
                {"path": "/copied/path.pkl"},
            ]
            db_ctx.artifacts.get_jobs.return_value = {"produced_by": [], "consumed_by": []}

            result = runner.invoke(show, [full_hash], obj=mock_ctx)

        assert result.exit_code == 0
        assert "Locations" in result.output
        assert "/original/path.pkl" in result.output
        assert "/copied/path.pkl" in result.output

    def test_show_artifact_displays_producer_jobs(self, runner, mock_ctx):
        """Artifact display shows jobs that created the artifact."""
        full_hash = "a1b2c3d4e5f67890" * 4

        with patch.object(show_module, "create_database_context") as mock_db:
            db_ctx = MagicMock()
            mock_db.return_value.__enter__.return_value = db_ctx

            db_ctx.jobs.get_by_uid.return_value = None
            db_ctx.artifacts.get_by_hash.return_value = {
                "id": "artifact-prod",
                "size": 1024,
                "first_seen_at": 1700000000.0,
                "first_seen_path": None,
                "hashes": [{"algorithm": "blake3", "digest": full_hash}],
            }
            db_ctx.artifacts.get_locations.return_value = []
            db_ctx.artifacts.get_jobs.return_value = {
                "produced_by": [
                    {"job_uid": "job1234", "command": "python train.py"},
                ],
                "consumed_by": [],
            }

            result = runner.invoke(show, [full_hash], obj=mock_ctx)

        assert result.exit_code == 0
        assert "Produced by" in result.output
        assert "job1234" in result.output
        assert "python train.py" in result.output

    def test_show_artifact_displays_consumer_jobs(self, runner, mock_ctx):
        """Artifact display shows jobs that used the artifact as input."""
        full_hash = "a1b2c3d4e5f67890" * 4

        with patch.object(show_module, "create_database_context") as mock_db:
            db_ctx = MagicMock()
            mock_db.return_value.__enter__.return_value = db_ctx

            db_ctx.jobs.get_by_uid.return_value = None
            db_ctx.artifacts.get_by_hash.return_value = {
                "id": "artifact-cons",
                "size": 1024,
                "first_seen_at": 1700000000.0,
                "first_seen_path": None,
                "hashes": [{"algorithm": "blake3", "digest": full_hash}],
            }
            db_ctx.artifacts.get_locations.return_value = []
            db_ctx.artifacts.get_jobs.return_value = {
                "produced_by": [],
                "consumed_by": [
                    {"job_uid": "evaluid1", "command": "python evaluate.py"},
                ],
            }

            result = runner.invoke(show, [full_hash], obj=mock_ctx)

        assert result.exit_code == 0
        assert "Consumed by" in result.output
        assert "evaluid1" in result.output
        assert "python evaluate.py" in result.output


class TestShowArtifactByPath:
    """Tests for path-based artifact lookup."""

    @pytest.fixture
    def runner(self):
        """Create a Click CLI test runner."""
        return CliRunner()

    @pytest.fixture
    def mock_ctx(self, tmp_path):
        """Create a mock RoarContext."""
        ctx = MagicMock()
        ctx.roar_dir = tmp_path / ".roar"
        ctx.roar_dir.mkdir()
        ctx.cwd = tmp_path
        ctx.is_initialized = True
        return ctx

    def test_show_artifact_by_absolute_path(self, runner, mock_ctx):
        """/data/model.pkl absolute path works."""
        abs_path = "/data/model.pkl"

        with patch.object(show_module, "create_database_context") as mock_db:
            db_ctx = MagicMock()
            mock_db.return_value.__enter__.return_value = db_ctx

            db_ctx.artifacts.get_by_path.return_value = {
                "id": "artifact-abs",
                "size": 1024,
                "first_seen_at": 1700000000.0,
                "first_seen_path": abs_path,
                "hashes": [{"algorithm": "blake3", "digest": "hash" * 16}],
            }
            db_ctx.artifacts.get_locations.return_value = [{"path": abs_path}]
            db_ctx.artifacts.get_jobs.return_value = {"produced_by": [], "consumed_by": []}

            result = runner.invoke(show, [abs_path], obj=mock_ctx)

        assert result.exit_code == 0
        assert "Artifact:" in result.output

    def test_show_artifact_by_relative_path(self, runner, mock_ctx):
        """./data/model.pkl relative path works."""
        rel_path = "./data/model.pkl"

        with patch.object(show_module, "create_database_context") as mock_db:
            db_ctx = MagicMock()
            mock_db.return_value.__enter__.return_value = db_ctx

            db_ctx.artifacts.get_by_path.return_value = {
                "id": "artifact-rel",
                "size": 2048,
                "first_seen_at": 1700000000.0,
                "first_seen_path": str(mock_ctx.cwd / "data/model.pkl"),
                "hashes": [{"algorithm": "blake3", "digest": "hash" * 16}],
            }
            db_ctx.artifacts.get_locations.return_value = []
            db_ctx.artifacts.get_jobs.return_value = {"produced_by": [], "consumed_by": []}

            result = runner.invoke(show, [rel_path], obj=mock_ctx)

        assert result.exit_code == 0
        assert "Artifact:" in result.output

    def test_show_artifact_relative_path_resolved_to_absolute(self, runner, mock_ctx):
        """Relative paths are resolved to absolute paths for database lookup."""
        rel_path = "./data/model.pkl"
        # Expected: cwd/data/model.pkl (without the ./)
        expected_abs_path = str(mock_ctx.cwd / "data" / "model.pkl")

        with patch.object(show_module, "create_database_context") as mock_db:
            db_ctx = MagicMock()
            mock_db.return_value.__enter__.return_value = db_ctx

            db_ctx.artifacts.get_by_path.return_value = None

            runner.invoke(show, [rel_path], obj=mock_ctx)

        # Verify get_by_path was called with the resolved absolute path
        db_ctx.artifacts.get_by_path.assert_called_once_with(expected_abs_path)

    def test_show_artifact_path_not_tracked(self, runner, mock_ctx):
        """Shows 'not found' for paths not in the database."""
        unknown_path = "/unknown/file.txt"

        with patch.object(show_module, "create_database_context") as mock_db:
            db_ctx = MagicMock()
            mock_db.return_value.__enter__.return_value = db_ctx

            db_ctx.artifacts.get_by_path.return_value = None

            result = runner.invoke(show, [unknown_path], obj=mock_ctx)

        assert result.exit_code == 0
        assert "No artifact found" in result.output or "not found" in result.output.lower()

    def test_show_artifact_bare_filename_existing_file(self, runner, mock_ctx, tmp_path):
        """model.pkl in current directory works as file path."""
        # Create an actual file
        test_file = tmp_path / "model.pkl"
        test_file.touch()

        with patch.object(show_module, "create_database_context") as mock_db:
            db_ctx = MagicMock()
            mock_db.return_value.__enter__.return_value = db_ctx

            db_ctx.artifacts.get_by_path.return_value = {
                "id": "artifact-bare",
                "size": 512,
                "first_seen_at": 1700000000.0,
                "first_seen_path": str(test_file),
                "hashes": [{"algorithm": "blake3", "digest": "hash" * 16}],
            }
            db_ctx.artifacts.get_locations.return_value = []
            db_ctx.artifacts.get_jobs.return_value = {"produced_by": [], "consumed_by": []}

            result = runner.invoke(show, ["model.pkl"], obj=mock_ctx)

        assert result.exit_code == 0
        assert "Artifact:" in result.output

    def test_show_artifact_path_with_dotdot_normalized(self, runner, mock_ctx):
        """Paths with ../ components are normalized without following symlinks."""
        # Path like /tmp/foo/../bar should become /tmp/bar
        path_with_dotdot = str(mock_ctx.cwd / "subdir" / ".." / "data" / "model.pkl")
        expected_normalized = str(mock_ctx.cwd / "data" / "model.pkl")

        with patch.object(show_module, "create_database_context") as mock_db:
            db_ctx = MagicMock()
            mock_db.return_value.__enter__.return_value = db_ctx

            db_ctx.artifacts.get_by_path.return_value = None

            runner.invoke(show, [path_with_dotdot], obj=mock_ctx)

        # Verify get_by_path was called with the normalized path (no ../)
        db_ctx.artifacts.get_by_path.assert_called_once_with(expected_normalized)

    def test_show_artifact_symlink_not_resolved(self, runner, mock_ctx, tmp_path):
        """Symlinks are looked up by their own path, not the resolved target."""
        # Create a real file and a symlink to it
        real_file = tmp_path / "real_data" / "model.pkl"
        real_file.parent.mkdir()
        real_file.touch()

        symlink_path = tmp_path / "link_to_model.pkl"
        symlink_path.symlink_to(real_file)

        with patch.object(show_module, "create_database_context") as mock_db:
            db_ctx = MagicMock()
            mock_db.return_value.__enter__.return_value = db_ctx

            db_ctx.artifacts.get_by_path.return_value = None

            runner.invoke(show, [str(symlink_path)], obj=mock_ctx)

        # Should look up by symlink path, NOT the resolved real file path
        db_ctx.artifacts.get_by_path.assert_called_once_with(str(symlink_path))


class TestShowReferenceDisambiguation:
    """Tests for correct reference type detection."""

    @pytest.fixture
    def runner(self):
        """Create a Click CLI test runner."""
        return CliRunner()

    @pytest.fixture
    def mock_ctx(self, tmp_path):
        """Create a mock RoarContext."""
        ctx = MagicMock()
        ctx.roar_dir = tmp_path / ".roar"
        ctx.roar_dir.mkdir()
        ctx.cwd = tmp_path
        ctx.is_initialized = True
        return ctx

    def test_job_ref_takes_precedence_for_8char_hex(self, runner, mock_ctx):
        """8-char hex string tries job lookup first."""
        job_uid = "a1b2c3d4"  # 8 chars

        with patch.object(show_module, "create_database_context") as mock_db:
            db_ctx = MagicMock()
            mock_db.return_value.__enter__.return_value = db_ctx

            # Session is active
            db_ctx.sessions.get_active.return_value = {"id": 1, "hash": "session123"}

            # Job found by UID
            db_ctx.jobs.get_by_uid.return_value = {
                "id": 1,
                "job_uid": job_uid,
                "step_number": 1,
                "timestamp": 1700000000.0,
                "duration_seconds": 10.0,
                "exit_code": 0,
                "command": "python train.py",
                "job_type": None,
                "step_name": None,
                "step_identity": None,
                "git_commit": None,
                "git_branch": None,
                "metadata": None,
                "telemetry": None,
            }
            db_ctx.jobs.get_inputs.return_value = []
            db_ctx.jobs.get_outputs.return_value = []

            result = runner.invoke(show, [job_uid], obj=mock_ctx)

        assert result.exit_code == 0
        assert "Job:" in result.output
        assert job_uid in result.output

    def test_artifact_hash_used_when_job_not_found(self, runner, mock_ctx):
        """Long hex string falls back to artifact when no job found."""
        long_hash = "a1b2c3d4e5f67890"  # 16 chars

        with patch.object(show_module, "create_database_context") as mock_db:
            db_ctx = MagicMock()
            mock_db.return_value.__enter__.return_value = db_ctx

            # No job found
            db_ctx.jobs.get_by_uid.return_value = None

            # Artifact found
            db_ctx.artifacts.get_by_hash.return_value = {
                "id": "artifact-fallback",
                "size": 1024,
                "first_seen_at": 1700000000.0,
                "first_seen_path": None,
                "hashes": [{"algorithm": "blake3", "digest": long_hash + "x" * 48}],
            }
            db_ctx.artifacts.get_locations.return_value = []
            db_ctx.artifacts.get_jobs.return_value = {"produced_by": [], "consumed_by": []}

            result = runner.invoke(show, [long_hash], obj=mock_ctx)

        assert result.exit_code == 0
        assert "Artifact:" in result.output

    def test_at_notation_always_job_ref(self, runner, mock_ctx):
        """@1 is always treated as a job step reference."""
        with patch.object(show_module, "create_database_context") as mock_db:
            db_ctx = MagicMock()
            mock_db.return_value.__enter__.return_value = db_ctx

            db_ctx.sessions.get_active.return_value = {"id": 1, "hash": "session123"}
            db_ctx.sessions.get_step_by_number.return_value = {
                "id": 1,
                "job_uid": "stepjob1",
                "step_number": 1,
                "timestamp": 1700000000.0,
                "duration_seconds": 5.0,
                "exit_code": 0,
                "command": "python step1.py",
                "job_type": None,
                "step_name": None,
                "step_identity": None,
                "git_commit": None,
                "git_branch": None,
                "metadata": None,
                "telemetry": None,
            }
            db_ctx.jobs.get_inputs.return_value = []
            db_ctx.jobs.get_outputs.return_value = []

            result = runner.invoke(show, ["@1"], obj=mock_ctx)

        assert result.exit_code == 0
        assert "Job:" in result.output
        db_ctx.sessions.get_step_by_number.assert_called()

    def test_path_with_slash_always_file_lookup(self, runner, mock_ctx):
        """data/file.csv with slash is treated as a path."""
        path_ref = "data/file.csv"

        with patch.object(show_module, "create_database_context") as mock_db:
            db_ctx = MagicMock()
            mock_db.return_value.__enter__.return_value = db_ctx

            db_ctx.artifacts.get_by_path.return_value = {
                "id": "artifact-path",
                "size": 256,
                "first_seen_at": 1700000000.0,
                "first_seen_path": str(mock_ctx.cwd / path_ref),
                "hashes": [{"algorithm": "blake3", "digest": "hash" * 16}],
            }
            db_ctx.artifacts.get_locations.return_value = []
            db_ctx.artifacts.get_jobs.return_value = {"produced_by": [], "consumed_by": []}

            result = runner.invoke(show, [path_ref], obj=mock_ctx)

        assert result.exit_code == 0
        # Should call get_by_path, not get_by_hash
        db_ctx.artifacts.get_by_path.assert_called()


class TestShowEdgeCases:
    """Edge cases for the show command."""

    @pytest.fixture
    def runner(self):
        """Create a Click CLI test runner."""
        return CliRunner()

    @pytest.fixture
    def mock_ctx(self, tmp_path):
        """Create a mock RoarContext."""
        ctx = MagicMock()
        ctx.roar_dir = tmp_path / ".roar"
        ctx.roar_dir.mkdir()
        ctx.cwd = tmp_path
        ctx.is_initialized = True
        return ctx

    def test_show_artifact_with_no_jobs(self, runner, mock_ctx):
        """Artifact without any lineage displays OK."""
        full_hash = "a1b2c3d4e5f67890" * 4

        with patch.object(show_module, "create_database_context") as mock_db:
            db_ctx = MagicMock()
            mock_db.return_value.__enter__.return_value = db_ctx

            db_ctx.jobs.get_by_uid.return_value = None
            db_ctx.artifacts.get_by_hash.return_value = {
                "id": "artifact-nojobs",
                "size": 1024,
                "first_seen_at": 1700000000.0,
                "first_seen_path": "/some/path.txt",
                "hashes": [{"algorithm": "blake3", "digest": full_hash}],
            }
            db_ctx.artifacts.get_locations.return_value = [{"path": "/some/path.txt"}]
            db_ctx.artifacts.get_jobs.return_value = {"produced_by": [], "consumed_by": []}

            result = runner.invoke(show, [full_hash], obj=mock_ctx)

        assert result.exit_code == 0
        assert "Artifact:" in result.output
        # Should not have "Produced by" or "Consumed by" sections when empty
        # (or they can be present but empty, both are valid)

    def test_show_existing_job_behavior_unchanged(self, runner, mock_ctx):
        """Existing @N notation still works for job lookup."""
        with patch.object(show_module, "create_database_context") as mock_db:
            db_ctx = MagicMock()
            mock_db.return_value.__enter__.return_value = db_ctx

            db_ctx.sessions.get_active.return_value = {"id": 1, "hash": "session123"}
            db_ctx.sessions.get_step_by_number.return_value = {
                "id": 1,
                "job_uid": "existing1",
                "step_number": 1,
                "timestamp": 1700000000.0,
                "duration_seconds": 10.0,
                "exit_code": 0,
                "command": "python existing.py",
                "job_type": None,
                "step_name": None,
                "step_identity": None,
                "git_commit": None,
                "git_branch": None,
                "metadata": None,
                "telemetry": None,
            }
            db_ctx.jobs.get_inputs.return_value = []
            db_ctx.jobs.get_outputs.return_value = []

            result = runner.invoke(show, ["@1"], obj=mock_ctx)

        assert result.exit_code == 0
        assert "Job:" in result.output
        assert "existing1" in result.output

    def test_show_session_overview_unchanged(self, runner, mock_ctx):
        """No args still shows session overview."""
        with patch.object(show_module, "create_database_context") as mock_db:
            db_ctx = MagicMock()
            mock_db.return_value.__enter__.return_value = db_ctx

            db_ctx.sessions.get_active.return_value = {
                "id": 1,
                "hash": "session123abc",
                "created_at": 1700000000.0,
                "git_repo": "https://github.com/test/repo",
                "git_commit_start": "abc123",
            }
            db_ctx.jobs.get_by_session.return_value = []

            result = runner.invoke(show, [], obj=mock_ctx)

        assert result.exit_code == 0
        assert "Session:" in result.output
        assert "session123abc" in result.output

    def test_show_job_displays_packages_by_manager(self, runner, mock_ctx):
        """Job with dict-based packages displays grouped by package manager."""
        import json

        metadata = json.dumps(
            {
                "packages": {
                    "pip": {"numpy": "1.24.1", "pandas": "2.0.0"},
                    "dpkg": {"libcudnn8": "8.6.0"},
                    "build_dpkg": {"gcc-12": "12.3.0"},
                }
            }
        )

        with patch.object(show_module, "create_database_context") as mock_db:
            db_ctx = MagicMock()
            mock_db.return_value.__enter__ = MagicMock(return_value=db_ctx)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)

            db_ctx.sessions.get_active.return_value = {"id": 1, "hash": "sess1"}
            db_ctx.sessions.get_step_by_number.return_value = {
                "id": 1,
                "job_uid": "pkg-job-1",
                "step_number": 1,
                "timestamp": 1700000000.0,
                "duration_seconds": 10.0,
                "exit_code": 0,
                "command": "python train.py",
                "job_type": None,
                "step_name": None,
                "step_identity": None,
                "git_commit": None,
                "git_branch": None,
                "metadata": metadata,
                "telemetry": None,
            }
            db_ctx.jobs.get_inputs.return_value = []
            db_ctx.jobs.get_outputs.return_value = []

            result = runner.invoke(show, ["@1"], obj=mock_ctx)

        assert result.exit_code == 0
        assert "Packages (pip, 2)" in result.output
        assert "numpy==1.24.1" in result.output
        assert "pandas==2.0.0" in result.output
        assert "Packages (dpkg, 1)" in result.output
        assert "libcudnn8==8.6.0" in result.output
        assert "Packages (build_dpkg, 1)" in result.output
        assert "gcc-12==12.3.0" in result.output
