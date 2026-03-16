"""
Unit tests for GCS download backend.

Tests use mocked google-cloud-storage to avoid requiring GCP credentials.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mock_gcs():
    """Mock google-cloud-storage for GCS backend tests."""
    with patch("roar.integrations.download.gcs.storage", None):
        mock_storage = MagicMock()
        mock_client = MagicMock()
        mock_bucket = MagicMock()
        mock_storage.Client.return_value = mock_client
        mock_client.bucket.return_value = mock_bucket

        import roar.integrations.download.gcs as gcs_mod

        gcs_mod.storage = mock_storage

        yield mock_client, mock_bucket


class TestGCSDownloadBackend:
    """Tests for GCS download backend."""

    def test_download_calls_gcs(self, mock_gcs, tmp_path: Path):
        """Download calls blob.download_to_filename."""
        mock_client, mock_bucket = mock_gcs
        mock_blob = MagicMock()
        mock_blob.exists.return_value = True
        mock_bucket.blob.return_value = mock_blob

        from roar.integrations.download.gcs import GCSDownloadBackend

        backend = GCSDownloadBackend.__new__(GCSDownloadBackend)
        backend._bucket_name = "my-bucket"
        backend._client = mock_client
        backend._bucket = mock_bucket

        local_path = tmp_path / "model.pt"
        backend.download("models/model.pt", local_path)

        mock_bucket.blob.assert_called_with("models/model.pt")
        mock_blob.download_to_filename.assert_called_once_with(str(local_path))

    def test_download_raises_on_missing_blob(self, mock_gcs, tmp_path: Path):
        """Download raises FileNotFoundError when blob doesn't exist."""
        mock_client, mock_bucket = mock_gcs
        mock_blob = MagicMock()
        mock_blob.exists.return_value = False
        mock_bucket.blob.return_value = mock_blob

        from roar.integrations.download.gcs import GCSDownloadBackend

        backend = GCSDownloadBackend.__new__(GCSDownloadBackend)
        backend._bucket_name = "my-bucket"
        backend._client = mock_client
        backend._bucket = mock_bucket

        with pytest.raises(FileNotFoundError, match="GCS blob not found"):
            backend.download("missing.pt", tmp_path / "missing.pt")

    def test_exists_true(self, mock_gcs):
        """exists returns True when blob exists."""
        mock_client, mock_bucket = mock_gcs
        mock_blob = MagicMock()
        mock_blob.exists.return_value = True
        mock_bucket.blob.return_value = mock_blob

        from roar.integrations.download.gcs import GCSDownloadBackend

        backend = GCSDownloadBackend.__new__(GCSDownloadBackend)
        backend._bucket_name = "my-bucket"
        backend._client = mock_client
        backend._bucket = mock_bucket

        assert backend.exists("model.pt") is True

    def test_exists_false(self, mock_gcs):
        """exists returns False when blob doesn't exist."""
        mock_client, mock_bucket = mock_gcs
        mock_blob = MagicMock()
        mock_blob.exists.return_value = False
        mock_bucket.blob.return_value = mock_blob

        from roar.integrations.download.gcs import GCSDownloadBackend

        backend = GCSDownloadBackend.__new__(GCSDownloadBackend)
        backend._bucket_name = "my-bucket"
        backend._client = mock_client
        backend._bucket = mock_bucket

        assert backend.exists("missing.pt") is False

    def test_list_keys_returns_blobs(self, mock_gcs):
        """list_keys returns all blob names under prefix."""
        mock_client, mock_bucket = mock_gcs

        blob1 = MagicMock()
        blob1.name = "data/train.csv"
        blob2 = MagicMock()
        blob2.name = "data/test.csv"
        mock_client.list_blobs.return_value = [blob1, blob2]

        from roar.integrations.download.gcs import GCSDownloadBackend

        backend = GCSDownloadBackend.__new__(GCSDownloadBackend)
        backend._bucket_name = "my-bucket"
        backend._client = mock_client
        backend._bucket = mock_bucket

        keys = backend.list_keys("data")
        assert keys == ["data/train.csv", "data/test.csv"]

    def test_list_keys_skips_directory_markers(self, mock_gcs):
        """list_keys skips blobs ending with / (directory markers)."""
        mock_client, mock_bucket = mock_gcs

        blob1 = MagicMock()
        blob1.name = "data/"
        blob2 = MagicMock()
        blob2.name = "data/file.csv"
        mock_client.list_blobs.return_value = [blob1, blob2]

        from roar.integrations.download.gcs import GCSDownloadBackend

        backend = GCSDownloadBackend.__new__(GCSDownloadBackend)
        backend._bucket_name = "my-bucket"
        backend._client = mock_client
        backend._bucket = mock_bucket

        keys = backend.list_keys("data")
        assert keys == ["data/file.csv"]
