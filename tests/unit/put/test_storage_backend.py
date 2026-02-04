"""
Unit tests for storage backends.

Tests the abstract interface and memory backend for testing.
"""

from pathlib import Path

import pytest

from roar.services.put.backends import MemoryBackend, parse_destination


class TestParseDestination:
    """Tests for destination URL parsing."""

    def test_parse_s3_url(self):
        """Parse S3 URL extracts bucket and prefix."""
        result = parse_destination("s3://my-bucket/path/to/artifacts")

        assert result.scheme == "s3"
        assert result.bucket == "my-bucket"
        assert result.prefix == "path/to/artifacts"

    def test_parse_s3_url_no_prefix(self):
        """Parse S3 URL with no prefix."""
        result = parse_destination("s3://my-bucket")

        assert result.scheme == "s3"
        assert result.bucket == "my-bucket"
        assert result.prefix == ""

    def test_parse_gs_url(self):
        """Parse GCS URL extracts bucket and prefix."""
        result = parse_destination("gs://my-bucket/models")

        assert result.scheme == "gs"
        assert result.bucket == "my-bucket"
        assert result.prefix == "models"

    def test_parse_invalid_scheme_raises(self):
        """Parsing unsupported scheme raises error."""
        with pytest.raises(ValueError) as exc_info:
            parse_destination("ftp://server/path")

        assert "Unsupported" in str(exc_info.value)
        assert "ftp" in str(exc_info.value)


class TestMemoryBackend:
    """Tests for in-memory storage backend (for testing)."""

    def test_upload_stores_content(self, tmp_path: Path):
        """Upload stores file content in memory."""
        # Arrange
        test_file = tmp_path / "model.pt"
        test_file.write_bytes(b"model data")
        backend = MemoryBackend(bucket="test-bucket", prefix="models")

        # Act
        url = backend.upload(test_file, "model.pt")

        # Assert
        assert url == "memory://test-bucket/models/model.pt"
        assert backend.exists("model.pt")
        assert backend.get_content("model.pt") == b"model data"

    def test_upload_preserves_nested_path(self, tmp_path: Path):
        """Upload preserves nested path structure."""
        # Arrange
        nested_dir = tmp_path / "checkpoints"
        nested_dir.mkdir()
        test_file = nested_dir / "epoch_10.pt"
        test_file.write_bytes(b"checkpoint")
        backend = MemoryBackend(bucket="bucket", prefix="run-42")

        # Act
        url = backend.upload(test_file, "checkpoints/epoch_10.pt")

        # Assert
        assert url == "memory://bucket/run-42/checkpoints/epoch_10.pt"
        assert backend.exists("checkpoints/epoch_10.pt")

    def test_exists_returns_false_for_missing(self):
        """Exists returns False for non-uploaded files."""
        backend = MemoryBackend(bucket="bucket", prefix="")

        assert backend.exists("missing.pt") is False

    def test_upload_multiple_files(self, tmp_path: Path):
        """Upload multiple files independently."""
        # Arrange
        file1 = tmp_path / "model.pt"
        file2 = tmp_path / "config.yaml"
        file1.write_bytes(b"model")
        file2.write_bytes(b"config")
        backend = MemoryBackend(bucket="bucket", prefix="")

        # Act
        backend.upload(file1, "model.pt")
        backend.upload(file2, "config.yaml")

        # Assert
        assert backend.exists("model.pt")
        assert backend.exists("config.yaml")
        assert backend.get_content("model.pt") == b"model"
        assert backend.get_content("config.yaml") == b"config"

    def test_list_uploaded_files(self, tmp_path: Path):
        """List returns all uploaded file keys."""
        # Arrange
        file1 = tmp_path / "a.pt"
        file2 = tmp_path / "b.pt"
        file1.write_bytes(b"a")
        file2.write_bytes(b"b")
        backend = MemoryBackend(bucket="bucket", prefix="")

        backend.upload(file1, "a.pt")
        backend.upload(file2, "b.pt")

        # Act
        keys = backend.list_keys()

        # Assert
        assert set(keys) == {"a.pt", "b.pt"}
