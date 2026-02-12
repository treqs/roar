"""
Get service orchestrator.

Coordinates the full get workflow: parse source URL, download files,
hash them, register artifacts locally, and create a job record.

roar get is LOCAL ONLY — no GLaaS registration. Artifacts appear in GLaaS
naturally when downstream jobs consume them and get registered via roar put
or roar register.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...core.logging import get_logger
from ..transfer import DatabaseContext, build_operation_metadata_json, hash_files_blake3
from .backends.base import DownloadBackend, Source


@dataclass
class GetResult:
    """Result of a get operation."""

    success: bool
    job_id: int | None = None
    job_uid: str | None = None
    downloaded_files: list[dict[str, Any]] = field(default_factory=list)
    dry_run: bool = False
    would_download: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


class GetService:
    """
    Orchestrates the get workflow.

    1. Parse source URL to select backend
    2. Download files (single file or prefix listing)
    3. Compute BLAKE3 hash during download
    4. Verify hash if --hash provided
    5. Register artifacts locally
    6. Create job record with outputs
    """

    def __init__(
        self,
        db_context: DatabaseContext,
        backend: DownloadBackend,
        source: Source,
        repo_root: Path | None = None,
    ):
        """
        Initialize get service.

        Args:
            db_context: Database context for artifact/job operations.
            backend: Download backend for fetching files.
            source: Parsed source URL.
            repo_root: Repository root for path resolution.
        """
        self._db = db_context
        self._backend = backend
        self._source = source
        self._repo_root = Path(repo_root) if repo_root else Path.cwd()
        self._logger = get_logger()

        self._logger.debug(
            "GetService initialized: source=%s, repo_root=%s, backend=%s",
            source.original_url,
            self._repo_root,
            type(backend).__name__,
        )

    def get(
        self,
        destination: Path,
        message: str | None = None,
        expected_hash: str | None = None,
        dry_run: bool = False,
        force: bool = False,
        git_commit: str | None = None,
        git_tag: str | None = None,
        is_prefix: bool = False,
    ) -> GetResult:
        """
        Execute a get operation.

        Args:
            destination: Local path to download to.
            message: Optional annotation message.
            expected_hash: Expected BLAKE3 hash (fails if mismatch).
            dry_run: If True, show what would be done without doing it.
            force: If True, overwrite existing files.
            git_commit: Git commit SHA at time of download.
            git_tag: Git tag created for this download.
            is_prefix: If True, treat source as a prefix and download all files.

        Returns:
            GetResult with operation details.

        Raises:
            ValueError: If no active session.
            FileExistsError: If destination exists and force is False.
        """
        self._logger.debug(
            "get() called: destination=%s, message=%r, expected_hash=%s, "
            "dry_run=%s, force=%s, is_prefix=%s",
            destination,
            message,
            expected_hash,
            dry_run,
            force,
            is_prefix,
        )

        # Determine if this is a prefix/directory download
        if is_prefix or self._source.is_prefix:
            return self._get_prefix(
                destination=destination,
                message=message,
                dry_run=dry_run,
                force=force,
                git_commit=git_commit,
                git_tag=git_tag,
            )
        else:
            return self._get_single(
                destination=destination,
                message=message,
                expected_hash=expected_hash,
                dry_run=dry_run,
                force=force,
                git_commit=git_commit,
                git_tag=git_tag,
            )

    def _get_single(
        self,
        destination: Path,
        message: str | None = None,
        expected_hash: str | None = None,
        dry_run: bool = False,
        force: bool = False,
        git_commit: str | None = None,
        git_tag: str | None = None,
    ) -> GetResult:
        """Download a single file."""
        remote_key = self._source.key

        # Determine final path
        final_path = self._resolve_destination(destination, self._source.filename)

        # Check if file exists
        if final_path.exists() and not force:
            raise FileExistsError(
                f"Destination already exists: {final_path}. Use --force to overwrite."
            )

        if dry_run:
            return GetResult(
                success=True,
                dry_run=True,
                would_download=[
                    {
                        "remote_url": self._source.original_url,
                        "local_path": str(final_path),
                    }
                ],
            )

        # Download to temp file, then move
        start_time = time.time()
        final_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = final_path.with_suffix(final_path.suffix + ".roar_tmp")

        try:
            self._backend.download(remote_key, tmp_path)

            # Hash the downloaded file
            file_hash = self._hash_files_batch([tmp_path])[str(tmp_path)]
            file_size = tmp_path.stat().st_size

            # Verify hash if expected
            if expected_hash and file_hash != expected_hash:
                tmp_path.unlink()
                return GetResult(
                    success=False,
                    error=(f"Hash mismatch: expected {expected_hash}, got {file_hash}"),
                )

            # Move to final destination
            if final_path.exists():
                final_path.unlink()
            tmp_path.rename(final_path)

        except Exception as e:
            # Clean up tmp file on error
            if tmp_path.exists():
                tmp_path.unlink()
            self._logger.debug("Single-file get failed for %s: %s", remote_key, e)
            raise

        file_info = {
            "remote_url": self._source.original_url,
            "local_path": str(final_path),
            "hash": file_hash,
            "size": file_size,
        }

        duration = time.time() - start_time

        # Register artifact and create job
        return self._record_download(
            files=[file_info],
            message=message,
            git_commit=git_commit,
            git_tag=git_tag,
            duration_seconds=duration,
        )

    def _get_prefix(
        self,
        destination: Path,
        message: str | None = None,
        dry_run: bool = False,
        force: bool = False,
        git_commit: str | None = None,
        git_tag: str | None = None,
    ) -> GetResult:
        """Download all files under a prefix."""
        prefix = self._source.key
        self._logger.debug("Listing keys under prefix: %s", prefix)

        keys = self._backend.list_keys(prefix)
        if not keys:
            return GetResult(
                success=False,
                error=f"No files found under prefix: {self._source.original_url}",
            )

        self._logger.debug("Found %d key(s) under prefix", len(keys))

        if dry_run:
            would_download = []
            for key in keys:
                relative = self._relative_to_prefix(key, prefix)
                local_path = destination / relative
                remote_url = f"{self._source.scheme}://{self._source.bucket}/{key}"
                would_download.append({"remote_url": remote_url, "local_path": str(local_path)})
            return GetResult(
                success=True,
                dry_run=True,
                would_download=would_download,
            )

        # Download each file then hash all files in one batch.
        start_time = time.time()
        downloaded_files: list[dict[str, Any]] = []
        pending_downloads: list[dict[str, Any]] = []
        try:
            for key in keys:
                relative = self._relative_to_prefix(key, prefix)
                local_path = destination / relative

                if local_path.exists() and not force:
                    raise FileExistsError(
                        f"Destination already exists: {local_path}. Use --force to overwrite."
                    )

                local_path.parent.mkdir(parents=True, exist_ok=True)
                tmp_path = local_path.with_suffix(local_path.suffix + ".roar_tmp")
                self._backend.download(key, tmp_path)
                remote_url = f"{self._source.scheme}://{self._source.bucket}/{key}"
                pending_downloads.append(
                    {
                        "tmp_path": tmp_path,
                        "local_path": local_path,
                        "remote_url": remote_url,
                        "relative_key": relative,
                    }
                )

            hashes_by_tmp_path = self._hash_files_batch(
                [entry["tmp_path"] for entry in pending_downloads]
            )

            for entry in pending_downloads:
                tmp_path = entry["tmp_path"]
                local_path = entry["local_path"]
                remote_url = entry["remote_url"]
                relative = entry["relative_key"]
                key = str(tmp_path)
                if key not in hashes_by_tmp_path:
                    raise OSError(f"Failed to hash downloaded file: {tmp_path}")

                file_hash = hashes_by_tmp_path[key]
                file_size = tmp_path.stat().st_size

                if local_path.exists():
                    local_path.unlink()
                tmp_path.rename(local_path)

                downloaded_files.append(
                    {
                        "remote_url": remote_url,
                        "local_path": str(local_path),
                        "hash": file_hash,
                        "size": file_size,
                        "relative_key": relative,
                    }
                )
        except Exception as e:
            # Clean up any pending temp files on failure.
            for entry in pending_downloads:
                tmp_path = entry["tmp_path"]
                if tmp_path.exists():
                    tmp_path.unlink()
            self._logger.debug("Prefix get failed for %s: %s", prefix, e)
            raise

        duration = time.time() - start_time

        return self._record_download(
            files=downloaded_files,
            message=message,
            git_commit=git_commit,
            git_tag=git_tag,
            duration_seconds=duration,
        )

    def _record_download(
        self,
        files: list[dict[str, Any]],
        message: str | None = None,
        git_commit: str | None = None,
        git_tag: str | None = None,
        duration_seconds: float = 0.0,
    ) -> GetResult:
        """Register artifacts and create a job record for the download."""
        # Check for active session
        active_session = self._db.sessions.get_active()
        if active_session is None:
            raise ValueError("No active session")

        session_id = active_session["id"]
        self._logger.debug("Active session: id=%s", session_id)

        # Build metadata
        artifact_urls: dict[str, str] = {}
        artifacts_info: list[tuple[str, str]] = []  # (artifact_id, path)

        for file_info in files:
            # Register artifact locally
            artifact_id, created = self._db.artifacts.register(
                hashes={"blake3": file_info["hash"]},
                size=file_info["size"],
                path=file_info["local_path"],
            )
            self._logger.debug(
                "Artifact %s: id=%s (%s)",
                file_info["hash"][:12],
                artifact_id,
                "created" if created else "existing",
            )
            artifact_urls[artifact_id] = file_info["remote_url"]
            artifacts_info.append((artifact_id, file_info["local_path"]))

        # Determine source type
        source_type = self._source.scheme

        # Build command string
        command = f"roar get {self._source.original_url}"
        if message:
            command += f' -m "{message}"'

        # Build job metadata
        metadata_json = build_operation_metadata_json(
            "get",
            {
                "source": self._source.original_url,
                "source_type": source_type,
                "message": message,
                "artifacts": artifact_urls,
                "git_commit": git_commit,
                "git_tag": git_tag,
                "timestamp": time.time(),
            },
        )

        # Create job record
        step_number = self._db.sessions.get_next_step_number(session_id)
        job_id, job_uid = self._db.jobs.create(
            command=command,
            timestamp=time.time(),
            session_id=session_id,
            step_number=step_number,
            metadata=metadata_json,
            job_type="get",
            exit_code=0,
            duration_seconds=duration_seconds,
        )
        self._logger.debug(
            "Job created: id=%s, uid=%s, step=%d",
            job_id,
            job_uid,
            step_number,
        )

        # Link artifacts as OUTPUTS (get is a source node)
        for artifact_id, path in artifacts_info:
            self._db.jobs.add_output(job_id, artifact_id, path)
        self._logger.debug("Linked %d artifact(s) as job outputs", len(artifacts_info))

        return GetResult(
            success=True,
            job_id=job_id,
            job_uid=job_uid,
            downloaded_files=files,
        )

    def _resolve_destination(self, destination: Path, filename: str) -> Path:
        """Resolve the final destination path for a single file download."""
        if destination.is_dir() or str(destination).endswith("/"):
            # Download into directory with original filename
            destination.mkdir(parents=True, exist_ok=True)
            return destination / filename
        return destination

    @staticmethod
    def _relative_to_prefix(key: str, prefix: str) -> str:
        """Compute relative path of key within prefix."""
        if prefix and key.startswith(prefix):
            relative = key[len(prefix) :]
            return relative.lstrip("/")
        return key

    @staticmethod
    def _hash_files_batch(paths: list[Path]) -> dict[str, str]:
        """Compute BLAKE3 hash for paths in one backend batch call."""
        return hash_files_blake3(paths)
