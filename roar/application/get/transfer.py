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

from ...core.logging import get_logger
from ...db.hashing import hash_files_blake3
from ...integrations.download.base import DownloadBackend, Source
from ...presenters.spinner import Spinner
from .results import GetDownloadedFile, GetDryRunItem


def _download_progress_message(done: int, total: int, downloaded_bytes: int) -> str:
    """Status line for the download spinner, e.g. ``Downloading 47/100 · 4.31 GB``."""
    return f"Downloading {done}/{total} files · {downloaded_bytes / 1e9:.2f} GB"


@dataclass
class GetTransferResult:
    """Mechanical result of a get transfer."""

    success: bool
    downloaded_files: list[GetDownloadedFile] = field(default_factory=list)
    dry_run: bool = False
    would_download: list[GetDryRunItem] = field(default_factory=list)
    error: str | None = None


@dataclass(frozen=True)
class _PendingDownload:
    """Internal download plan for a single file."""

    remote_key: str
    remote_url: str
    local_path: Path
    tmp_path: Path
    relative_key: str | None = None


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
        limit: int | None = None,
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
                limit=limit,
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
        pending_downloads = [
            self._prepare_pending_download(
                remote_key=self._source.key,
                remote_url=self._source.original_url,
                local_path=self._resolve_destination(destination, self._source.filename),
                force=force,
            )
        ]

        if dry_run:
            return self._build_dry_run_result(pending_downloads)

        return self._execute_pending_downloads(
            pending_downloads,
            failure_context=f"Single-file get failed for {self._source.key}",
            expected_hash=expected_hash,
        )

    def _get_prefix(
        self,
        destination: Path,
        dry_run: bool = False,
        force: bool = False,
        limit: int | None = None,
    ) -> GetTransferResult:
        """Download all files under a prefix (or the first N data files when limited)."""
        import posixpath

        from ...application.composite.detect import BOILERPLATE

        prefix = self._source.key
        self._logger.debug("Listing keys under prefix: %s", prefix)

        # Listing a large repo (e.g. HF datasets paginate thousands of files) can take
        # several seconds before the first byte downloads.
        with Spinner("Fetching manifest..."):
            keys = self._backend.list_keys(prefix)
        if not keys:
            return GetTransferResult(
                success=False,
                error=f"No files found under prefix: {self._source.original_url}",
            )

        if limit is not None and limit >= 0:
            # Subset get: the first N *data* files (repo/format boilerplate excluded).
            data_keys = [k for k in keys if posixpath.basename(k) not in BOILERPLATE]
            keys = data_keys[:limit]

        self._logger.debug("Found %d key(s) under prefix", len(keys))

        pending_downloads = [
            self._prepare_pending_download(
                remote_key=key,
                remote_url=self._build_remote_url(key),
                local_path=destination / self._relative_to_prefix(key, prefix),
                force=force,
                relative_key=self._relative_to_prefix(key, prefix),
            )
            for key in keys
        ]

        if dry_run:
            return self._build_dry_run_result(pending_downloads)

        return self._execute_pending_downloads(
            pending_downloads,
            failure_context=f"Prefix get failed for {prefix}",
        )

    def _prepare_pending_download(
        self,
        *,
        remote_key: str,
        remote_url: str,
        local_path: Path,
        force: bool,
        relative_key: str | None = None,
    ) -> _PendingDownload:
        if local_path.exists() and not force:
            raise FileExistsError(
                f"Destination already exists: {local_path}. Use --force to overwrite."
            )

        local_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = local_path.with_suffix(local_path.suffix + ".roar_tmp")
        return _PendingDownload(
            remote_key=remote_key,
            remote_url=remote_url,
            local_path=local_path,
            tmp_path=tmp_path,
            relative_key=relative_key,
        )

    def _download_to_tmp(self, pending: _PendingDownload) -> None:
        self._backend.download(pending.remote_key, pending.tmp_path)

    @staticmethod
    def _finalize_download(pending: _PendingDownload) -> None:
        if pending.local_path.exists():
            pending.local_path.unlink()
        pending.tmp_path.rename(pending.local_path)

    @staticmethod
    def _build_dry_run_entry(pending: _PendingDownload) -> GetDryRunItem:
        return GetDryRunItem(
            remote_url=pending.remote_url,
            local_path=str(pending.local_path),
        )

    @staticmethod
    def _build_downloaded_file_entry(
        pending: _PendingDownload,
        file_hash: str,
        file_size: int,
    ) -> GetDownloadedFile:
        return GetDownloadedFile(
            remote_url=pending.remote_url,
            local_path=str(pending.local_path),
            hash=file_hash,
            size=file_size,
        )

    def _build_dry_run_result(
        self,
        pending_downloads: list[_PendingDownload],
    ) -> GetTransferResult:
        return GetTransferResult(
            success=True,
            dry_run=True,
            would_download=[self._build_dry_run_entry(pending) for pending in pending_downloads],
        )

    def _execute_pending_downloads(
        self,
        pending_downloads: list[_PendingDownload],
        *,
        failure_context: str,
        expected_hash: str | None = None,
    ) -> GetTransferResult:
        downloaded_files: list[GetDownloadedFile] = []
        total = len(pending_downloads)
        try:
            # Downloads are the long pole of a get (multi-GB datasets); show a live
            # N/M + cumulative-bytes counter so the terminal isn't dead for minutes.
            downloaded_bytes = 0
            with Spinner(_download_progress_message(0, total, 0)) as spin:
                for index, pending in enumerate(pending_downloads, start=1):
                    self._download_to_tmp(pending)
                    downloaded_bytes += pending.tmp_path.stat().st_size
                    spin.update(_download_progress_message(index, total, downloaded_bytes))

            with Spinner(f"Hashing {total} file(s)..."):
                hashes_by_tmp_path = self._hash_files_batch(
                    [entry.tmp_path for entry in pending_downloads]
                )

            for entry in pending_downloads:
                key = str(entry.tmp_path)
                if key not in hashes_by_tmp_path:
                    raise OSError(f"Failed to hash downloaded file: {entry.tmp_path}")

                file_hash = hashes_by_tmp_path[key]
                file_size = entry.tmp_path.stat().st_size
                if expected_hash and file_hash != expected_hash:
                    entry.tmp_path.unlink()
                    return GetTransferResult(
                        success=False,
                        error=(f"Hash mismatch: expected {expected_hash}, got {file_hash}"),
                    )
                self._finalize_download(entry)
                downloaded_files.append(
                    self._build_downloaded_file_entry(entry, file_hash, file_size)
                )
        except Exception as e:
            self._cleanup_tmp_files(pending_downloads)
            self._logger.debug("%s: %s", failure_context, e)
            raise

        return GetTransferResult(
            success=True,
            downloaded_files=downloaded_files,
        )

    @staticmethod
    def _cleanup_tmp_files(pending_downloads: list[_PendingDownload]) -> None:
        for pending in pending_downloads:
            if pending.tmp_path.exists():
                pending.tmp_path.unlink()

    def _build_remote_url(self, key: str) -> str:
        return f"{self._source.scheme}://{self._source.bucket}/{key}"

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
