"""
Google Cloud Storage download backend.

Downloads artifacts from GCS.
"""

from pathlib import Path

from .base import DownloadBackend

# Lazy import to avoid ImportError when not installed
storage = None


def _ensure_gcs():
    """Ensure google-cloud-storage is available, raising ImportError if not."""
    global storage
    if storage is None:
        from google.cloud import storage as _storage

        storage = _storage


class GCSDownloadBackend(DownloadBackend):
    """
    Google Cloud Storage download backend.

    Uses google-cloud-storage to download files from GCS. Credentials are resolved
    via Application Default Credentials (ADC).
    """

    def __init__(self, bucket: str):
        """
        Initialize GCS download backend.

        Args:
            bucket: GCS bucket name.

        Raises:
            ImportError: If google-cloud-storage is not installed.
        """
        _ensure_gcs()
        assert storage is not None
        self._bucket_name = bucket
        self._client = storage.Client()
        self._bucket = self._client.bucket(bucket)

    def download(self, remote_key: str, local_path: Path) -> None:
        """
        Download a file from GCS.

        Args:
            remote_key: Full GCS blob name.
            local_path: Local path to save the file to.

        Raises:
            FileNotFoundError: If the blob doesn't exist.
            google.api_core.exceptions.GoogleAPIError: If download fails.
        """
        blob = self._bucket.blob(remote_key)
        if not blob.exists():
            raise FileNotFoundError(f"GCS blob not found: gs://{self._bucket_name}/{remote_key}")
        local_path.parent.mkdir(parents=True, exist_ok=True)
        blob.download_to_filename(str(local_path))

    def exists(self, remote_key: str) -> bool:
        """
        Check if a blob exists in GCS.

        Args:
            remote_key: Full GCS blob name.

        Returns:
            True if the blob exists, False otherwise.
        """
        blob = self._bucket.blob(remote_key)
        return blob.exists()

    def list_keys(self, prefix: str) -> list[str]:
        """
        List all blobs under a prefix in GCS.

        Args:
            prefix: GCS blob name prefix.

        Returns:
            List of full blob names under the prefix.
        """
        # Ensure prefix ends with / for directory-like listing
        list_prefix = prefix
        if list_prefix and not list_prefix.endswith("/"):
            list_prefix += "/"

        keys: list[str] = []
        blobs = self._client.list_blobs(self._bucket_name, prefix=list_prefix)
        for blob in blobs:
            # Skip "directory" markers
            if not blob.name.endswith("/"):
                keys.append(blob.name)
        return keys
