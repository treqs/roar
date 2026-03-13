"""
Get service transfer mechanics.

Coordinates the mechanical get workflow: download files,
hash them, and materialize them locally.

roar get is LOCAL ONLY — no GLaaS registration. Artifacts appear in GLaaS
naturally when downstream jobs consume them and get registered via roar put
or roar register.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...core.logging import get_logger
from ...db.hashing import hash_files_blake3
from ...integrations.download.base import DownloadBackend, Source


@dataclass
class GetTransferResult:
    """Mechanical result of a get transfer."""

    success: bool
    downloaded_files: list[dict[str, Any]] = field(default_factory=list)
    dry_run: bool = False
    would_download: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


class GetService:
    """
    Executes `get` transfer mechanics.

    1. Download files (single file or prefix listing)
    2. Compute BLAKE3 hash during download
    3. Verify hash if --hash provided
    4. Return downloaded file facts for higher-level persistence
    """

    def __init__(
        self,
        backend: DownloadBackend,
        source: Source,
        repo_root: Path | None = None,
    ):
        """
        Initialize get service.

        Args:
            backend: Download backend for fetching files.
            source: Parsed source URL.
            repo_root: Repository root for path resolution.
        """
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
        expected_hash: str | None = None,
        dry_run: bool = False,
        force: bool = False,
        is_prefix: bool = False,
    ) -> GetTransferResult:
        """
        Execute a get operation.

        Args:
            destination: Local path to download to.
            expected_hash: Expected BLAKE3 hash (fails if mismatch).
            dry_run: If True, show what would be done without doing it.
            force: If True, overwrite existing files.
            is_prefix: If True, treat source as a prefix and download all files.

        Returns:
            GetTransferResult with transfer details.

        Raises:
            FileExistsError: If destination exists and force is False.
        """
        self._logger.debug(
            "get() called: destination=%s, expected_hash=%s, dry_run=%s, force=%s, is_prefix=%s",
            destination,
            expected_hash,
            dry_run,
            force,
            is_prefix,
        )

        # Determine if this is a prefix/directory download
        if is_prefix or self._source.is_prefix:
            return self._get_prefix(
                destination=destination,
                dry_run=dry_run,
                force=force,
            )
        else:
            return self._get_single(
                destination=destination,
                expected_hash=expected_hash,
                dry_run=dry_run,
                force=force,
            )

    def _get_single(
        self,
        destination: Path,
        expected_hash: str | None = None,
        dry_run: bool = False,
        force: bool = False,
    ) -> GetTransferResult:
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
            return GetTransferResult(
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
                return GetTransferResult(
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

        return GetTransferResult(
            success=True,
            downloaded_files=[file_info],
        )

    def _get_prefix(
        self,
        destination: Path,
        dry_run: bool = False,
        force: bool = False,
    ) -> GetTransferResult:
        """Download all files under a prefix."""
        prefix = self._source.key
        self._logger.debug("Listing keys under prefix: %s", prefix)

        keys = self._backend.list_keys(prefix)
        if not keys:
            return GetTransferResult(
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
            return GetTransferResult(
                success=True,
                dry_run=True,
                would_download=would_download,
            )

        # Download each file then hash all files in one batch.
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

        return GetTransferResult(
            success=True,
            downloaded_files=downloaded_files,
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
