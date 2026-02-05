"""
Google Cloud Storage backend.

Uploads artifacts to GCS.
"""

from pathlib import Path
from typing import TYPE_CHECKING

from .base import StorageBackend

if TYPE_CHECKING:
    pass

# Lazy import to avoid ImportError when not installed
storage = None


def _ensure_gcs():
    """Ensure google-cloud-storage is available, raising ImportError if not."""
    global storage
    if storage is None:
        from google.cloud import storage as _storage

        storage = _storage


class GCSBackend(StorageBackend):
    """
    Google Cloud Storage backend.

    Uses google-cloud-storage to upload files to GCS. Credentials are resolved
    via Application Default Credentials (ADC): GOOGLE_APPLICATION_CREDENTIALS
    env var, gcloud auth, workload identity, etc.
    """

    def __init__(self, bucket: str, prefix: str = ""):
        """
        Initialize GCS backend.

        Args:
            bucket: GCS bucket name.
            prefix: Key prefix for all uploads.

        Raises:
            ImportError: If google-cloud-storage is not installed.
        """
        _ensure_gcs()
        assert storage is not None  # guaranteed by _ensure_gcs()
        self._bucket_name = bucket
        self._prefix = prefix
        self._client = storage.Client()
        self._bucket = self._client.bucket(bucket)

    def _full_key(self, remote_key: str) -> str:
        """Build full GCS blob name with prefix."""
        if self._prefix:
            return f"{self._prefix}/{remote_key}"
        return remote_key

    def upload(self, local_path: Path, remote_key: str) -> str:
        """
        Upload a file to GCS.

        Args:
            local_path: Path to the local file.
            remote_key: Key/path in GCS (prefix will be prepended).

        Returns:
            Full GCS URL of the uploaded file.

        Raises:
            google.api_core.exceptions.GoogleAPIError: If upload fails.
        """
        full_key = self._full_key(remote_key)
        blob = self._bucket.blob(full_key)
        blob.upload_from_filename(str(local_path))
        return f"gs://{self._bucket_name}/{full_key}"

    def exists(self, remote_key: str) -> bool:
        """
        Check if a file exists in GCS.

        Args:
            remote_key: Key/path in GCS (prefix will be prepended).

        Returns:
            True if the file exists, False otherwise.
        """
        full_key = self._full_key(remote_key)
        blob = self._bucket.blob(full_key)
        return blob.exists()
