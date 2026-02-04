"""
Unit tests for GCS storage backend.

Uses mocking to avoid real GCS calls.
"""

from pathlib import Path
from unittest.mock import Mock, patch

import pytest

# Skip all tests in this module if google-cloud-storage is not installed
google_cloud_storage = pytest.importorskip("google.cloud.storage")

from roar.services.put.backends.gcs import GCSBackend  # noqa: E402


class TestGCSBackendUpload:
    """Tests for GCS upload functionality."""

    def test_upload_single_file(self, tmp_path: Path):
        """Upload a single file to GCS."""
        # Arrange
        test_file = tmp_path / "model.pt"
        test_file.write_bytes(b"model data")

        with patch("roar.services.put.backends.gcs.storage") as mock_storage:
            mock_client = Mock()
            mock_bucket = Mock()
            mock_blob = Mock()

            mock_storage.Client.return_value = mock_client
            mock_client.bucket.return_value = mock_bucket
            mock_bucket.blob.return_value = mock_blob

            backend = GCSBackend(bucket="my-bucket", prefix="models")

            # Act
            url = backend.upload(test_file, "model.pt")

            # Assert
            assert url == "gs://my-bucket/models/model.pt"
            mock_client.bucket.assert_called_once_with("my-bucket")
            mock_bucket.blob.assert_called_once_with("models/model.pt")
            mock_blob.upload_from_filename.assert_called_once_with(str(test_file))

    def test_upload_no_prefix(self, tmp_path: Path):
        """Upload to bucket root when no prefix specified."""
        test_file = tmp_path / "model.pt"
        test_file.write_bytes(b"model data")

        with patch("roar.services.put.backends.gcs.storage") as mock_storage:
            mock_client = Mock()
            mock_bucket = Mock()
            mock_blob = Mock()

            mock_storage.Client.return_value = mock_client
            mock_client.bucket.return_value = mock_bucket
            mock_bucket.blob.return_value = mock_blob

            backend = GCSBackend(bucket="my-bucket", prefix="")

            url = backend.upload(test_file, "model.pt")

            assert url == "gs://my-bucket/model.pt"
            mock_bucket.blob.assert_called_once_with("model.pt")

    def test_upload_nested_key(self, tmp_path: Path):
        """Upload with nested key preserves path structure."""
        test_file = tmp_path / "checkpoint.pt"
        test_file.write_bytes(b"checkpoint")

        with patch("roar.services.put.backends.gcs.storage") as mock_storage:
            mock_client = Mock()
            mock_bucket = Mock()
            mock_blob = Mock()

            mock_storage.Client.return_value = mock_client
            mock_client.bucket.return_value = mock_bucket
            mock_bucket.blob.return_value = mock_blob

            backend = GCSBackend(bucket="bucket", prefix="run-42")

            url = backend.upload(test_file, "checkpoints/epoch_10.pt")

            assert url == "gs://bucket/run-42/checkpoints/epoch_10.pt"
            mock_bucket.blob.assert_called_once_with("run-42/checkpoints/epoch_10.pt")


class TestGCSBackendExists:
    """Tests for GCS exists functionality."""

    def test_exists_returns_true_when_blob_exists(self):
        """Exists returns True when blob is in GCS."""
        with patch("roar.services.put.backends.gcs.storage") as mock_storage:
            mock_client = Mock()
            mock_bucket = Mock()
            mock_blob = Mock()

            mock_storage.Client.return_value = mock_client
            mock_client.bucket.return_value = mock_bucket
            mock_bucket.blob.return_value = mock_blob
            mock_blob.exists.return_value = True

            backend = GCSBackend(bucket="bucket", prefix="models")

            result = backend.exists("model.pt")

            assert result is True
            mock_bucket.blob.assert_called_with("models/model.pt")
            mock_blob.exists.assert_called_once()

    def test_exists_returns_false_when_blob_missing(self):
        """Exists returns False when blob is not in GCS."""
        with patch("roar.services.put.backends.gcs.storage") as mock_storage:
            mock_client = Mock()
            mock_bucket = Mock()
            mock_blob = Mock()

            mock_storage.Client.return_value = mock_client
            mock_client.bucket.return_value = mock_bucket
            mock_bucket.blob.return_value = mock_blob
            mock_blob.exists.return_value = False

            backend = GCSBackend(bucket="bucket", prefix="models")

            result = backend.exists("missing.pt")

            assert result is False


class TestGCSBackendAuth:
    """Tests for GCS authentication handling."""

    def test_uses_application_default_credentials(self):
        """Backend uses Application Default Credentials (ADC)."""
        with patch("roar.services.put.backends.gcs.storage") as mock_storage:
            mock_client = Mock()
            mock_storage.Client.return_value = mock_client

            GCSBackend(bucket="bucket", prefix="")

            # Verify client was created (ADC is used by default)
            mock_storage.Client.assert_called_once()

    def test_upload_auth_error_propagates(self, tmp_path: Path):
        """Auth errors during upload propagate to caller."""
        from google.api_core.exceptions import Forbidden

        test_file = tmp_path / "model.pt"
        test_file.write_bytes(b"data")

        with patch("roar.services.put.backends.gcs.storage") as mock_storage:
            mock_client = Mock()
            mock_bucket = Mock()
            mock_blob = Mock()

            mock_storage.Client.return_value = mock_client
            mock_client.bucket.return_value = mock_bucket
            mock_bucket.blob.return_value = mock_blob
            mock_blob.upload_from_filename.side_effect = Forbidden("Access denied")

            backend = GCSBackend(bucket="bucket", prefix="")

            with pytest.raises(Forbidden):
                backend.upload(test_file, "model.pt")
