"""
Unit tests for S3 storage backend.

Uses mocking to avoid real AWS calls.
"""

from pathlib import Path
from unittest.mock import Mock, patch

import pytest

# Skip all tests in this module if boto3 is not installed
boto3 = pytest.importorskip("boto3")
botocore = pytest.importorskip("botocore")

from roar.services.put.backends.s3 import S3Backend  # noqa: E402


class TestS3BackendUpload:
    """Tests for S3 upload functionality."""

    def test_upload_single_file(self, tmp_path: Path):
        """Upload a single file to S3."""
        # Arrange
        test_file = tmp_path / "model.pt"
        test_file.write_bytes(b"model data")

        with patch("roar.services.put.backends.s3.boto3") as mock_boto3:
            mock_client = Mock()
            mock_boto3.client.return_value = mock_client

            backend = S3Backend(bucket="my-bucket", prefix="models")

            # Act
            url = backend.upload(test_file, "model.pt")

            # Assert
            assert url == "s3://my-bucket/models/model.pt"
            mock_client.upload_file.assert_called_once()
            call_args = mock_client.upload_file.call_args
            assert call_args[0][0] == str(test_file)  # local path
            assert call_args[0][1] == "my-bucket"  # bucket
            assert call_args[0][2] == "models/model.pt"  # key

    def test_upload_no_prefix(self, tmp_path: Path):
        """Upload to bucket root when no prefix specified."""
        test_file = tmp_path / "model.pt"
        test_file.write_bytes(b"model data")

        with patch("roar.services.put.backends.s3.boto3") as mock_boto3:
            mock_client = Mock()
            mock_boto3.client.return_value = mock_client

            backend = S3Backend(bucket="my-bucket", prefix="")

            url = backend.upload(test_file, "model.pt")

            assert url == "s3://my-bucket/model.pt"
            call_args = mock_client.upload_file.call_args
            assert call_args[0][2] == "model.pt"  # key without prefix

    def test_upload_nested_key(self, tmp_path: Path):
        """Upload with nested key preserves path structure."""
        test_file = tmp_path / "checkpoint.pt"
        test_file.write_bytes(b"checkpoint")

        with patch("roar.services.put.backends.s3.boto3") as mock_boto3:
            mock_client = Mock()
            mock_boto3.client.return_value = mock_client

            backend = S3Backend(bucket="bucket", prefix="run-42")

            url = backend.upload(test_file, "checkpoints/epoch_10.pt")

            assert url == "s3://bucket/run-42/checkpoints/epoch_10.pt"
            call_args = mock_client.upload_file.call_args
            assert call_args[0][2] == "run-42/checkpoints/epoch_10.pt"


class TestS3BackendExists:
    """Tests for S3 exists functionality."""

    def test_exists_returns_true_when_object_exists(self):
        """Exists returns True when object is in S3."""
        with patch("roar.services.put.backends.s3.boto3") as mock_boto3:
            mock_client = Mock()
            mock_boto3.client.return_value = mock_client
            # head_object succeeds = object exists
            mock_client.head_object.return_value = {"ContentLength": 100}

            backend = S3Backend(bucket="bucket", prefix="models")

            result = backend.exists("model.pt")

            assert result is True
            mock_client.head_object.assert_called_once_with(Bucket="bucket", Key="models/model.pt")

    def test_exists_returns_false_when_object_missing(self):
        """Exists returns False when object is not in S3."""
        with patch("roar.services.put.backends.s3.boto3") as mock_boto3:
            mock_client = Mock()
            mock_boto3.client.return_value = mock_client
            # head_object raises 404 = object doesn't exist
            from botocore.exceptions import ClientError

            mock_client.head_object.side_effect = ClientError(
                {"Error": {"Code": "404", "Message": "Not Found"}},
                "HeadObject",
            )

            backend = S3Backend(bucket="bucket", prefix="models")

            result = backend.exists("missing.pt")

            assert result is False


class TestS3BackendAuth:
    """Tests for S3 authentication handling."""

    def test_uses_default_credentials(self):
        """Backend uses default boto3 credential chain."""
        with patch("roar.services.put.backends.s3.boto3") as mock_boto3:
            mock_client = Mock()
            mock_boto3.client.return_value = mock_client

            S3Backend(bucket="bucket", prefix="")

            # Verify client was created with s3 service
            mock_boto3.client.assert_called_once_with("s3")

    def test_upload_auth_error_propagates(self, tmp_path: Path):
        """Auth errors during upload propagate to caller."""
        test_file = tmp_path / "model.pt"
        test_file.write_bytes(b"data")

        with patch("roar.services.put.backends.s3.boto3") as mock_boto3:
            from botocore.exceptions import ClientError

            mock_client = Mock()
            mock_boto3.client.return_value = mock_client
            mock_client.upload_file.side_effect = ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "Access Denied"}},
                "PutObject",
            )

            backend = S3Backend(bucket="bucket", prefix="")

            with pytest.raises(ClientError) as exc_info:
                backend.upload(test_file, "model.pt")

            assert "AccessDenied" in str(exc_info.value)
