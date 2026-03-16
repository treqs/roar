"""
AWS S3 storage backend.

Uploads artifacts to Amazon S3.
"""

from pathlib import Path
from typing import TYPE_CHECKING

from .base import StorageBackend

if TYPE_CHECKING:
    pass

# Lazy import boto3 to avoid ImportError when not installed
boto3 = None
ClientError = None


def _ensure_boto3():
    """Ensure boto3 is available, raising ImportError if not."""
    global boto3, ClientError
    if boto3 is None:
        import boto3 as _boto3
        from botocore.exceptions import ClientError as _ClientError

        boto3 = _boto3
        ClientError = _ClientError


class S3Backend(StorageBackend):
    """
    AWS S3 storage backend.

    Uses boto3 to upload files to S3. Credentials are resolved
    via the default boto3 credential chain (env vars, ~/.aws/credentials,
    IAM role, etc.).
    """

    def __init__(self, bucket: str, prefix: str = ""):
        """
        Initialize S3 backend.

        Args:
            bucket: S3 bucket name.
            prefix: Key prefix for all uploads.

        Raises:
            ImportError: If boto3 is not installed.
        """
        _ensure_boto3()
        assert boto3 is not None  # guaranteed by _ensure_boto3()
        self._bucket = bucket
        self._prefix = prefix
        self._client = boto3.client("s3")

    def _full_key(self, remote_key: str) -> str:
        """Build full S3 key with prefix."""
        if self._prefix:
            return f"{self._prefix}/{remote_key}"
        return remote_key

    def upload(self, local_path: Path, remote_key: str) -> str:
        """
        Upload a file to S3.

        Args:
            local_path: Path to the local file.
            remote_key: Key/path in S3 (prefix will be prepended).

        Returns:
            Full S3 URL of the uploaded file.

        Raises:
            ClientError: If upload fails (auth, network, etc.).
        """
        full_key = self._full_key(remote_key)
        self._client.upload_file(str(local_path), self._bucket, full_key)
        return f"s3://{self._bucket}/{full_key}"

    def exists(self, remote_key: str) -> bool:
        """
        Check if a file exists in S3.

        Args:
            remote_key: Key/path in S3 (prefix will be prepended).

        Returns:
            True if the file exists, False otherwise.
        """
        # Import here to get the lazily-loaded exception class
        from botocore.exceptions import ClientError as BotoClientError

        full_key = self._full_key(remote_key)
        try:
            self._client.head_object(Bucket=self._bucket, Key=full_key)
            return True
        except BotoClientError as e:
            if e.response["Error"]["Code"] == "404":
                return False
            raise
