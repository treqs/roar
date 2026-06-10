"""
Unit tests for S3 download backend.

Tests use mocked boto3 to avoid requiring AWS credentials.
"""

from unittest.mock import MagicMock, Mock, patch

from botocore.exceptions import ClientError


class TestS3DownloadBackend:
    """Tests for S3 download backend."""

    def test_exists_false_on_404(self):
        """exists returns False when head_object returns 404."""
        with (
            patch("roar.integrations.download.s3.boto3") as mock_boto3,
            patch("roar.integrations.download.s3.ClientError", ClientError),
        ):
            mock_client = Mock()
            mock_boto3.client.return_value = mock_client

            mock_client.head_object.side_effect = ClientError(
                {"Error": {"Code": "404", "Message": "Not Found"}},
                "HeadObject",
            )

            from roar.integrations.download.s3 import S3DownloadBackend

            backend = S3DownloadBackend(bucket="my-bucket")

            assert backend.exists("missing.pt") is False

    def test_list_keys_skips_directory_markers(self):
        """list_keys skips keys ending with / (directory markers)."""
        with patch("roar.integrations.download.s3.boto3") as mock_boto3:
            mock_client = Mock()
            mock_boto3.client.return_value = mock_client

            mock_paginator = MagicMock()
            mock_paginator.paginate.return_value = [
                {
                    "Contents": [
                        {"Key": "data/"},
                        {"Key": "data/train.csv"},
                        {"Key": "data/test.csv"},
                    ]
                }
            ]
            mock_client.get_paginator.return_value = mock_paginator

            from roar.integrations.download.s3 import S3DownloadBackend

            backend = S3DownloadBackend(bucket="my-bucket")

            keys = backend.list_keys("data")

            assert keys == ["data/train.csv", "data/test.csv"]

    def test_list_keys_empty_prefix(self):
        """list_keys handles empty results."""
        with patch("roar.integrations.download.s3.boto3") as mock_boto3:
            mock_client = Mock()
            mock_boto3.client.return_value = mock_client

            mock_paginator = MagicMock()
            mock_paginator.paginate.return_value = [{}]
            mock_client.get_paginator.return_value = mock_paginator

            from roar.integrations.download.s3 import S3DownloadBackend

            backend = S3DownloadBackend(bucket="my-bucket")

            keys = backend.list_keys("nonexistent")
            assert keys == []
