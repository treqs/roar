"""
Upload service interfaces.

Defines protocols for artifact upload and registration services.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass
class UploadResult:
    """Result of an upload operation."""

    success: bool
    artifacts_uploaded: int = 0
    artifacts_registered: int = 0
    lineage_jobs: int = 0
    error: str | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class LineageData:
    """Collected lineage data for upload."""

    jobs: list[dict] = field(default_factory=list)
    artifacts: list[dict] = field(default_factory=list)
    artifact_hashes: set[str] = field(default_factory=set)
    pipeline: dict | None = None


@runtime_checkable
class IUploadService(Protocol):
    """Protocol for upload orchestration service."""

    def upload_and_register(
        self,
        sources: list[Path],
        dest_url: str,
        force: bool,
        tag: str | None,
        message: str | None,
        roar_dir: Path,
    ) -> UploadResult:
        """
        Upload artifacts to cloud storage and register with GLaaS.

        Args:
            sources: List of source file/directory paths
            dest_url: Destination URL (s3://, gs://)
            force: Force upload even if already exists
            tag: Git tag for reproducibility
            message: Description message for GLaaS
            roar_dir: Path to .roar directory

        Returns:
            UploadResult with success status and counts
        """
        ...


@runtime_checkable
class ILineageCollector(Protocol):
    """Protocol for collecting lineage data."""

    def collect(
        self,
        artifact_hashes: list[str],
        roar_dir: Path,
    ) -> LineageData:
        """
        Collect lineage data for the given artifact hashes.

        Args:
            artifact_hashes: List of artifact hashes to trace
            roar_dir: Path to .roar directory

        Returns:
            LineageData with jobs and artifacts in the lineage
        """
        ...
