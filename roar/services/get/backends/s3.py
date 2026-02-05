"""
AWS S3 download backend.

Downloads artifacts from Amazon S3.
"""

from pathlib import Path

from .base import DownloadBackend

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


class S3DownloadBackend(DownloadBackend):
    """
    AWS S3 download backend.

    Uses boto3 to download files from S3. Credentials are resolved
    via the default boto3 credential chain (env vars, ~/.aws/credentials,
    IAM role, etc.).
    """

    def __init__(self, bucket: str):
        """
        Initialize S3 download backend.

        Args:
            bucket: S3 bucket name.

        Raises:
            ImportError: If boto3 is not installed.
        """
        _ensure_boto3()
        assert boto3 is not None
        self._bucket = bucket
        self._client = boto3.client("s3")

    def download(self, remote_key: str, local_path: Path) -> None:
        """
        Download a file from S3.

        Args:
            remote_key: Full S3 key (no prefix prepending).
            local_path: Local path to save the file to.

        Raises:
            FileNotFoundError: If the key doesn't exist.
            ClientError: If download fails.
        """
        from botocore.exceptions import ClientError as BotoClientError

        local_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._client.download_file(self._bucket, remote_key, str(local_path))
        except BotoClientError as e:
            if e.response["Error"]["Code"] == "404":
                raise FileNotFoundError(
                    f"S3 key not found: s3://{self._bucket}/{remote_key}"
                ) from e
            raise

    def exists(self, remote_key: str) -> bool:
        """
        Check if a key exists in S3.

        Args:
            remote_key: Full S3 key.

        Returns:
            True if the key exists, False otherwise.
        """
        from botocore.exceptions import ClientError as BotoClientError

        try:
            self._client.head_object(Bucket=self._bucket, Key=remote_key)
            return True
        except BotoClientError as e:
            if e.response["Error"]["Code"] == "404":
                return False
            raise

    def list_keys(self, prefix: str) -> list[str]:
        """
        List all keys under a prefix in S3.

        Args:
            prefix: S3 key prefix.

        Returns:
            List of full keys under the prefix.
        """
        keys: list[str] = []
        paginator = self._client.get_paginator("list_objects_v2")

        # Ensure prefix ends with / for directory-like listing
        list_prefix = prefix
        if list_prefix and not list_prefix.endswith("/"):
            list_prefix += "/"

        for page in paginator.paginate(Bucket=self._bucket, Prefix=list_prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                # Skip "directory" markers (keys ending with /)
                if not key.endswith("/"):
                    keys.append(key)

        return keys
