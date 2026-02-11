"""
Unit tests for the get service orchestrator.

Tests the end-to-end get workflow: download → hash → register → create job.
"""

import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from roar.services.get.backends.base import Source
from roar.services.get.backends.noop import NoOpDownloadBackend
from roar.services.get.service import GetService


def _make_mock_db():
    """Create a mock database context for testing."""
    mock_db = Mock()
    mock_db.artifacts = Mock()
    mock_db.artifacts.register.return_value = ("artifact-uuid-1", True)
    mock_db.jobs = Mock()
    mock_db.jobs.create.return_value = (42, "job-uid-1")
    mock_db.sessions = Mock()
    mock_db.sessions.get_active.return_value = {"id": 1}
    mock_db.sessions.get_next_step_number.return_value = 3
    return mock_db


def _make_source(url: str = "s3://my-bucket/models/model.pt") -> Source:
    """Create a Source object for testing."""
    from roar.services.get.backends.base import parse_source

    return parse_source(url)


class TestGetServiceSingleFile:
    """Tests for single file download."""

    def test_get_single_file_creates_job(self, tmp_path: Path):
        """Get a single file downloads and creates a job record."""
        # Arrange
        backend = NoOpDownloadBackend(bucket="my-bucket")
        backend.seed("models/model.pt", b"model data here")

        mock_db = _make_mock_db()
        source = _make_source("s3://my-bucket/models/model.pt")

        service = GetService(
            db_context=mock_db,
            backend=backend,
            source=source,
            repo_root=tmp_path,
        )

        dest = tmp_path / "model.pt"

        # Act
        result = service.get(destination=dest)

        # Assert
        assert result.success is True
        assert result.job_id == 42
        assert len(result.downloaded_files) == 1
        assert result.downloaded_files[0]["local_path"] == str(dest)
        assert dest.exists()
        assert dest.read_bytes() == b"model data here"

        # Verify job was created as "get" type
        mock_db.jobs.create.assert_called_once()
        call_kwargs = mock_db.jobs.create.call_args[1]
        assert call_kwargs["job_type"] == "get"
        assert "roar get" in call_kwargs["command"]
        assert call_kwargs["session_id"] == 1
        assert call_kwargs["step_number"] == 3

        # Verify artifact was linked as OUTPUT (not input!)
        mock_db.jobs.add_output.assert_called_once()
        mock_db.jobs.add_input.assert_not_called()

    def test_get_stores_metadata(self, tmp_path: Path):
        """Get stores source URL and type in job metadata."""
        backend = NoOpDownloadBackend(bucket="my-bucket")
        backend.seed("models/model.pt", b"data")

        mock_db = _make_mock_db()
        source = _make_source("s3://my-bucket/models/model.pt")

        service = GetService(
            db_context=mock_db,
            backend=backend,
            source=source,
            repo_root=tmp_path,
        )

        service.get(destination=tmp_path / "model.pt")

        call_kwargs = mock_db.jobs.create.call_args[1]
        metadata = json.loads(call_kwargs["metadata"])
        assert "get" in metadata
        assert metadata["get"]["source"] == "s3://my-bucket/models/model.pt"
        assert metadata["get"]["source_type"] == "s3"
        assert "artifacts" in metadata["get"]

    def test_get_with_message(self, tmp_path: Path):
        """Get with message stores it in metadata."""
        backend = NoOpDownloadBackend(bucket="my-bucket")
        backend.seed("model.pt", b"data")

        mock_db = _make_mock_db()
        source = _make_source("s3://my-bucket/model.pt")

        service = GetService(
            db_context=mock_db,
            backend=backend,
            source=source,
            repo_root=tmp_path,
        )

        service.get(
            destination=tmp_path / "model.pt",
            message="downloaded for testing",
        )

        metadata = json.loads(mock_db.jobs.create.call_args[1]["metadata"])
        assert metadata["get"]["message"] == "downloaded for testing"

    def test_get_without_message(self, tmp_path: Path):
        """Get without message stores None in metadata (message is optional)."""
        backend = NoOpDownloadBackend(bucket="my-bucket")
        backend.seed("model.pt", b"data")

        mock_db = _make_mock_db()
        source = _make_source("s3://my-bucket/model.pt")

        service = GetService(
            db_context=mock_db,
            backend=backend,
            source=source,
            repo_root=tmp_path,
        )

        service.get(destination=tmp_path / "model.pt")

        metadata = json.loads(mock_db.jobs.create.call_args[1]["metadata"])
        assert metadata["get"]["message"] is None

    def test_get_stores_git_context(self, tmp_path: Path):
        """Get stores git commit and tag in metadata."""
        backend = NoOpDownloadBackend(bucket="my-bucket")
        backend.seed("model.pt", b"data")

        mock_db = _make_mock_db()
        source = _make_source("s3://my-bucket/model.pt")

        service = GetService(
            db_context=mock_db,
            backend=backend,
            source=source,
            repo_root=tmp_path,
        )

        service.get(
            destination=tmp_path / "model.pt",
            git_commit="abc123",
            git_tag="roar/abc123",
        )

        metadata = json.loads(mock_db.jobs.create.call_args[1]["metadata"])
        assert metadata["get"]["git_commit"] == "abc123"
        assert metadata["get"]["git_tag"] == "roar/abc123"

    def test_get_computes_blake3_hash(self, tmp_path: Path):
        """Get computes BLAKE3 hash of downloaded file."""
        import blake3

        content = b"test model data"
        expected_hash = blake3.blake3(content).hexdigest()

        backend = NoOpDownloadBackend(bucket="my-bucket")
        backend.seed("model.pt", content)

        mock_db = _make_mock_db()
        source = _make_source("s3://my-bucket/model.pt")

        service = GetService(
            db_context=mock_db,
            backend=backend,
            source=source,
            repo_root=tmp_path,
        )

        result = service.get(destination=tmp_path / "model.pt")

        assert result.downloaded_files[0]["hash"] == expected_hash

        # Verify artifact was registered with the hash
        mock_db.artifacts.register.assert_called_once()
        reg_kwargs = mock_db.artifacts.register.call_args[1]
        assert reg_kwargs["hashes"]["blake3"] == expected_hash

    def test_get_hash_verification_passes(self, tmp_path: Path):
        """Get with matching --hash succeeds."""
        import blake3

        content = b"verified content"
        correct_hash = blake3.blake3(content).hexdigest()

        backend = NoOpDownloadBackend(bucket="my-bucket")
        backend.seed("model.pt", content)

        mock_db = _make_mock_db()
        source = _make_source("s3://my-bucket/model.pt")

        service = GetService(
            db_context=mock_db,
            backend=backend,
            source=source,
            repo_root=tmp_path,
        )

        result = service.get(
            destination=tmp_path / "model.pt",
            expected_hash=correct_hash,
        )

        assert result.success is True

    def test_get_hash_verification_fails(self, tmp_path: Path):
        """Get with mismatching --hash fails and deletes the file."""
        backend = NoOpDownloadBackend(bucket="my-bucket")
        backend.seed("model.pt", b"wrong content")

        mock_db = _make_mock_db()
        source = _make_source("s3://my-bucket/model.pt")

        service = GetService(
            db_context=mock_db,
            backend=backend,
            source=source,
            repo_root=tmp_path,
        )

        result = service.get(
            destination=tmp_path / "model.pt",
            expected_hash="0000000000000000000000000000000000000000",
        )

        assert result.success is False
        assert "Hash mismatch" in result.error
        # Temp file should be cleaned up
        assert not (tmp_path / "model.pt.roar_tmp").exists()

    def test_get_destination_into_directory(self, tmp_path: Path):
        """Get into a directory uses original filename."""
        backend = NoOpDownloadBackend(bucket="my-bucket")
        backend.seed("models/model.pt", b"data")

        mock_db = _make_mock_db()
        source = _make_source("s3://my-bucket/models/model.pt")

        service = GetService(
            db_context=mock_db,
            backend=backend,
            source=source,
            repo_root=tmp_path,
        )

        dest_dir = tmp_path / "output"
        dest_dir.mkdir()
        result = service.get(destination=dest_dir)

        expected_path = dest_dir / "model.pt"
        assert expected_path.exists()
        assert result.downloaded_files[0]["local_path"] == str(expected_path)


class TestGetServiceDryRun:
    """Tests for dry run mode."""

    def test_dry_run_does_not_download(self, tmp_path: Path):
        """Dry run doesn't download files or create job."""
        backend = NoOpDownloadBackend(bucket="my-bucket")

        mock_db = _make_mock_db()
        source = _make_source("s3://my-bucket/models/model.pt")

        service = GetService(
            db_context=mock_db,
            backend=backend,
            source=source,
            repo_root=tmp_path,
        )

        result = service.get(
            destination=tmp_path / "model.pt",
            dry_run=True,
        )

        assert result.success is True
        assert result.dry_run is True
        assert len(result.would_download) == 1
        assert result.would_download[0]["remote_url"] == "s3://my-bucket/models/model.pt"
        assert not (tmp_path / "model.pt").exists()
        mock_db.jobs.create.assert_not_called()


class TestGetServiceFileExists:
    """Tests for file exists behavior."""

    def test_raises_when_file_exists(self, tmp_path: Path):
        """Get raises FileExistsError when destination exists without --force."""
        backend = NoOpDownloadBackend(bucket="my-bucket")
        backend.seed("model.pt", b"data")

        mock_db = _make_mock_db()
        source = _make_source("s3://my-bucket/model.pt")

        service = GetService(
            db_context=mock_db,
            backend=backend,
            source=source,
            repo_root=tmp_path,
        )

        # Create existing file
        dest = tmp_path / "model.pt"
        dest.write_bytes(b"existing")

        with pytest.raises(FileExistsError, match="Use --force"):
            service.get(destination=dest)

    def test_force_overwrites_existing(self, tmp_path: Path):
        """Get with --force overwrites existing file."""
        backend = NoOpDownloadBackend(bucket="my-bucket")
        backend.seed("model.pt", b"new data")

        mock_db = _make_mock_db()
        source = _make_source("s3://my-bucket/model.pt")

        service = GetService(
            db_context=mock_db,
            backend=backend,
            source=source,
            repo_root=tmp_path,
        )

        dest = tmp_path / "model.pt"
        dest.write_bytes(b"old data")

        result = service.get(destination=dest, force=True)

        assert result.success is True
        assert dest.read_bytes() == b"new data"


class TestGetServicePrefix:
    """Tests for prefix/directory downloads."""

    def test_prefix_download_multiple_files(self, tmp_path: Path):
        """Prefix download downloads all files and creates single job."""
        backend = NoOpDownloadBackend(bucket="my-bucket")
        backend.seed("checkpoints/epoch_1.pt", b"epoch 1")
        backend.seed("checkpoints/epoch_2.pt", b"epoch 2")

        mock_db = _make_mock_db()
        # Need to return unique artifact IDs
        artifact_ids = iter([("art-1", True), ("art-2", True)])
        mock_db.artifacts.register.side_effect = lambda **kwargs: next(artifact_ids)

        source = Source(
            scheme="s3",
            bucket="my-bucket",
            key="checkpoints",
            original_url="s3://my-bucket/checkpoints/",
        )

        service = GetService(
            db_context=mock_db,
            backend=backend,
            source=source,
            repo_root=tmp_path,
        )

        dest = tmp_path / "local_checkpoints"
        result = service.get(destination=dest, is_prefix=True)

        assert result.success is True
        assert len(result.downloaded_files) == 2

        # Verify files exist with correct relative paths
        assert (dest / "epoch_1.pt").exists()
        assert (dest / "epoch_2.pt").exists()
        assert (dest / "epoch_1.pt").read_bytes() == b"epoch 1"
        assert (dest / "epoch_2.pt").read_bytes() == b"epoch 2"

        # Single job created
        mock_db.jobs.create.assert_called_once()

        # Both artifacts linked as outputs
        assert mock_db.jobs.add_output.call_count == 2

    def test_prefix_download_preserves_subdirs(self, tmp_path: Path):
        """Prefix download preserves subdirectory structure."""
        backend = NoOpDownloadBackend(bucket="my-bucket")
        backend.seed("data/train/images/img1.png", b"img1")
        backend.seed("data/train/labels/label1.txt", b"label1")

        mock_db = _make_mock_db()
        artifact_ids = iter([("art-1", True), ("art-2", True)])
        mock_db.artifacts.register.side_effect = lambda **kwargs: next(artifact_ids)

        source = Source(
            scheme="s3",
            bucket="my-bucket",
            key="data/train",
            original_url="s3://my-bucket/data/train/",
        )

        service = GetService(
            db_context=mock_db,
            backend=backend,
            source=source,
            repo_root=tmp_path,
        )

        dest = tmp_path / "training_data"
        result = service.get(destination=dest, is_prefix=True)

        assert result.success is True
        assert (dest / "images" / "img1.png").exists()
        assert (dest / "labels" / "label1.txt").exists()

    def test_prefix_no_files_returns_error(self, tmp_path: Path):
        """Prefix download with no matching files returns error."""
        backend = NoOpDownloadBackend(bucket="my-bucket")
        # Don't seed any files

        mock_db = _make_mock_db()
        source = Source(
            scheme="s3",
            bucket="my-bucket",
            key="nonexistent",
            original_url="s3://my-bucket/nonexistent/",
        )

        service = GetService(
            db_context=mock_db,
            backend=backend,
            source=source,
            repo_root=tmp_path,
        )

        result = service.get(destination=tmp_path / "output", is_prefix=True)

        assert result.success is False
        assert "No files found" in result.error

    def test_prefix_hashes_in_single_batch_call(self, tmp_path: Path):
        """Prefix download computes hashes for all files in one batch call."""
        import blake3

        backend = NoOpDownloadBackend(bucket="my-bucket")
        backend.seed("checkpoints/epoch_1.pt", b"epoch 1")
        backend.seed("checkpoints/epoch_2.pt", b"epoch 2")

        mock_db = _make_mock_db()
        artifact_ids = iter([("art-1", True), ("art-2", True)])
        mock_db.artifacts.register.side_effect = lambda **kwargs: next(artifact_ids)

        source = Source(
            scheme="s3",
            bucket="my-bucket",
            key="checkpoints",
            original_url="s3://my-bucket/checkpoints/",
        )
        service = GetService(
            db_context=mock_db,
            backend=backend,
            source=source,
            repo_root=tmp_path,
        )

        calls = {"count": 0}

        def fake_hashes(paths):
            calls["count"] += 1
            return {str(path): blake3.blake3(Path(path).read_bytes()).hexdigest() for path in paths}

        with patch("roar.services.get.service.hash_files_blake3", side_effect=fake_hashes):
            result = service.get(destination=tmp_path / "out", is_prefix=True)

        assert result.success is True
        assert calls["count"] == 1

    def test_prefix_dry_run(self, tmp_path: Path):
        """Prefix dry run lists all files that would be downloaded."""
        backend = NoOpDownloadBackend(bucket="my-bucket")
        backend.seed("data/a.csv", b"a")
        backend.seed("data/b.csv", b"b")

        mock_db = _make_mock_db()
        source = Source(
            scheme="s3",
            bucket="my-bucket",
            key="data",
            original_url="s3://my-bucket/data/",
        )

        service = GetService(
            db_context=mock_db,
            backend=backend,
            source=source,
            repo_root=tmp_path,
        )

        result = service.get(
            destination=tmp_path / "output",
            dry_run=True,
            is_prefix=True,
        )

        assert result.success is True
        assert result.dry_run is True
        assert len(result.would_download) == 2


class TestGetServiceErrors:
    """Tests for error handling."""

    def test_no_active_session_raises(self, tmp_path: Path):
        """Get without active session raises ValueError."""
        backend = NoOpDownloadBackend(bucket="my-bucket")
        backend.seed("model.pt", b"data")

        mock_db = _make_mock_db()
        mock_db.sessions.get_active.return_value = None

        source = _make_source("s3://my-bucket/model.pt")

        service = GetService(
            db_context=mock_db,
            backend=backend,
            source=source,
            repo_root=tmp_path,
        )

        with pytest.raises(ValueError, match="No active session"):
            service.get(destination=tmp_path / "model.pt")


class TestGetServiceRelativeKey:
    """Tests for relative key computation."""

    def test_relative_to_prefix(self):
        """_relative_to_prefix strips prefix correctly."""
        assert GetService._relative_to_prefix("data/train.csv", "data") == "train.csv"
        assert GetService._relative_to_prefix("data/sub/file.csv", "data") == "sub/file.csv"
        assert GetService._relative_to_prefix("data/train.csv", "data/") == "train.csv"
        assert GetService._relative_to_prefix("other/file.csv", "data") == "other/file.csv"
