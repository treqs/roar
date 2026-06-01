"""Get-specific download backend resolution."""

from __future__ import annotations

from ..resolution import load_backend_class, resolve_backend_for_scheme
from .base import DownloadBackend, parse_source
from .http import HTTPBackend
from .noop import NoOpDownloadBackend, should_skip_download


def resolve_download_backend(source_url: str) -> DownloadBackend:
    """Resolve the download backend used by `roar get`."""
    parsed = parse_source(source_url)

    if should_skip_download():
        return NoOpDownloadBackend(
            bucket=parsed.bucket,
            scheme=parsed.scheme,
        )

    builders = {
        "http": lambda: HTTPBackend(url=source_url),
        "https": lambda: HTTPBackend(url=source_url),
        "hf": lambda: load_backend_class(
            "roar.integrations.download.hf",
            "HFDownloadBackend",
            "HF backend is built in (stdlib)",
        )(source=parsed),
        "s3": lambda: load_backend_class(
            "roar.integrations.download.s3",
            "S3DownloadBackend",
            "S3 backend requires boto3. Install with: pip install boto3",
        )(bucket=parsed.bucket),
        "gs": lambda: load_backend_class(
            "roar.integrations.download.gcs",
            "GCSDownloadBackend",
            "GCS backend requires google-cloud-storage. Install with: pip install google-cloud-storage",
        )(bucket=parsed.bucket),
    }

    return resolve_backend_for_scheme(
        parsed.scheme,
        builders,
        f"Unsupported source scheme: {parsed.scheme}",
    )
