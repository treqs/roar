"""
File filter service for provenance collection.

Applies various filters to file lists based on configuration.
"""

import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from ...core.interfaces.logger import ILogger
from ...core.models.provenance import (
    FilterCounts,
    FilteredFiles,
    PythonInjectData,
    TracerData,
)

_FILTER_CATEGORIES: tuple[str, ...] = (
    "roar_internal",
    "git_metadata",
    "credential_files",
    "system_reads",
    "package_reads",
    "torch_cache",
    "library_caches",
    "tmp_files",
    "write_noise",
    "ignore_paths",
)


def _get_editable_install_dirs() -> frozenset[str]:
    """Return source dirs of all editable-installed packages."""
    dirs: set[str] = set()
    try:
        import importlib.metadata as importlib_metadata

        for dist in importlib_metadata.distributions():
            try:
                raw = dist.read_text("direct_url.json")
                if not raw:
                    continue
                data = json.loads(raw)
                if not data.get("dir_info", {}).get("editable"):
                    continue
                url = data.get("url", "")
                if not isinstance(url, str) or not url.startswith("file://"):
                    continue
                parsed = urlparse(url)
                source_dir = unquote(parsed.path)
                if not source_dir:
                    continue
                dirs.add(str(Path(source_dir).resolve()).rstrip("/") + "/")
            except Exception:
                continue
    except Exception:
        return frozenset()

    return frozenset(dirs)


class FileFilterService:
    """Filters files based on configuration settings."""

    # System paths to filter (for reads only)
    SYSTEM_READ_PREFIXES = (
        "/sys/",
        "/etc/",
        "/sbin/",
        "/proc/",
        "/dev/",
        "/run/",
        "/usr/",  # All of /usr/ (share, local, lib, bin, etc.)
        "/opt/",
        "/lib/",
        "/lib64/",
        # macOS system paths
        "/System/",
        "/Library/",
        "/Applications/",
        "/private/var/db/",
        "/private/var/run/",
        "/private/var/log/",
    )

    # Torch/triton cache patterns
    TORCH_CACHE_PATTERNS = (
        "/tmp/torchinductor_",
        "/tmp/torch_",
        "/tmp/triton",
    )

    # Curated set of per-library user-cache subpaths that are transient,
    # library-managed state rather than user-meaningful artifacts. Matched as
    # substrings so any home-dir variant (~/.cache/, /root/.cache/, /Users/<u>/
    # /Library/Caches/-style equivalents won't match — those are covered by
    # write-noise rules instead) is caught. A project that legitimately writes
    # outputs under ~/.cache/<your-project>/ stays tracked unless its
    # subpath is on this list.
    LIBRARY_CACHE_PATTERNS = (
        "/.cache/huggingface/",
        "/.cache/pip/",
        "/.cache/uv/",
        "/.cache/poetry/",
        "/.cache/pypoetry/",
        "/.cache/conda/",
        "/.cache/wandb/",
        "/.cache/mlflow/",
        "/.cache/pre-commit/",
        "/.cache/black/",
        "/.cache/mypy/",
        "/.cache/ruff/",
        "/.cache/pylint/",
        "/.cache/yarn/",
        "/.cache/npm/",
        "/.cache/Cypress/",
        "/.cache/torch/",  # complements TORCH_CACHE_PATTERNS (which covers /tmp)
        "/.cache/triton/",
        # Triton's JIT kernel cache defaults to ~/.triton/cache/ (NOT under
        # ~/.cache/), so it needs its own pattern — torch.compile/inductor on
        # GPU writes compiled .so launchers here every run.
        "/.triton/cache/",
        "/.cache/jupyter/",
        "/.cache/matplotlib/",
    )

    # Paths to always filter from written_files (not reproducibility-relevant)
    WRITE_NOISE_PREFIXES = (
        "/dev/",
        "/proc/",
        "/sys/",
        "/dev/shm/",
        "/tmp/roar-worker-env-",
        "/usr/local/",  # System-managed tools (e.g., aws-cli writes cacert.pem here)
        "/usr/lib/",
        "/usr/share/",
        "/opt/",
        # macOS system paths
        "/System/",
        "/Library/Caches/",
        "/private/var/db/",
        "/private/var/run/",
        "/private/var/log/",
    )

    def __init__(self, logger: ILogger | None = None) -> None:
        """
        Initialize file filter service.

        Args:
            logger: Logger for internal diagnostics
        """
        self._logger = logger

    @property
    def logger(self) -> ILogger:
        """Get logger, resolving from container or creating NullLogger."""
        if self._logger is None:
            from ...core.logging import get_logger

            self._logger = get_logger()
        return self._logger

    def filter_files(
        self,
        tracer_data: TracerData,
        python_data: PythonInjectData,
        config: dict[str, Any],
        *,
        collect_dropped_paths: bool = False,
    ) -> FilteredFiles:
        """
        Apply filters to file lists.

        Args:
            tracer_data: Loaded tracer data
            python_data: Loaded Python inject data
            config: Configuration dict (from .roar.toml)
            collect_dropped_paths: If True, also keep the per-category lists
                of paths that were filtered out, for `verbosity="debug"`
                rendering. Off by default — these lists can be very large
                (e.g. every package file an interpreter touches).

        Returns:
            FilteredFiles with filtered lists
        """
        self.logger.debug(
            "FileFilterService.filter_files: opened=%d, read=%d, written=%d",
            len(tracer_data.opened_files),
            len(tracer_data.read_files),
            len(tracer_data.written_files),
        )

        # Get filter settings
        filters_config = config.get("filters", {})
        ignore_system_reads = filters_config.get("ignore_system_reads", True)
        ignore_package_reads = filters_config.get("ignore_package_reads", True)
        ignore_torch_cache = filters_config.get("ignore_torch_cache", True)
        ignore_library_caches = filters_config.get("ignore_library_caches", True)
        ignore_tmp_files = filters_config.get("ignore_tmp_files", True)
        ignore_paths_raw: list[str] = filters_config.get("ignore_paths", [])
        # Build separate lists for absolute prefixes and repo-relative prefixes.
        ignore_abs: list[str] = []
        ignore_rel: list[str] = []
        for pattern in ignore_paths_raw:
            if pattern.startswith("/"):
                ignore_abs.append(pattern)
            else:
                ignore_rel.append(pattern)
        self.logger.debug(
            "Filter config: system_reads=%s, package_reads=%s, torch_cache=%s, "
            "library_caches=%s, tmp_files=%s, ignore_paths=%d",
            ignore_system_reads,
            ignore_package_reads,
            ignore_torch_cache,
            ignore_library_caches,
            ignore_tmp_files,
            len(ignore_paths_raw),
        )

        # Get cleanup settings
        cleanup_config = config.get("cleanup", {})
        delete_tmp_writes = cleanup_config.get("delete_tmp_writes", False)

        # Strict mode (delete_tmp_writes) overrides ignore_tmp_files
        if delete_tmp_writes:
            ignore_tmp_files = False

        # Create filter function for reads
        sys_prefix = python_data.sys_prefix
        sys_base_prefix = python_data.sys_base_prefix
        roar_inject_dir = python_data.roar_inject_dir
        editable_dirs = _get_editable_install_dirs()

        # Per-category drop counts. Always tracked. Per-category dropped
        # path lists are only built when collect_dropped_paths=True.
        counts: dict[str, int] = dict.fromkeys(_FILTER_CATEGORIES, 0)
        dropped: dict[str, list[str]] = (
            {cat: [] for cat in _FILTER_CATEGORIES} if collect_dropped_paths else {}
        )

        def _record_drop(category: str, path: str) -> None:
            counts[category] += 1
            if collect_dropped_paths:
                dropped[category].append(path)

        # A /tmp file the job *wrote* is an endogenous intermediate (created
        # under roar's watch) and is dropped as noise. A /tmp file the job only
        # *read* is a pre-existing input — ephemeral and almost certainly
        # unsourced, but a real dependency — so it is kept rather than silently
        # discarded, and surfaces through the unsourced-inputs warning. (An
        # in-place mmap edit reads *and* writes the file, so it lands in
        # written_set and is correctly treated as an intermediate.)
        written_set: set[str] = set(tracer_data.written_files)

        def categorize_read(path: str) -> str | None:
            """Return the filter category that drops this path, or None to keep."""
            if roar_inject_dir and path.startswith(roar_inject_dir):
                return "roar_internal"
            if "/roar-worker-env-" in path:
                return "roar_internal"
            if self._is_roar_internal(path):
                return "roar_internal"
            if self._is_git_metadata(path):
                return "git_metadata"
            if self._is_credential_file(path):
                return "credential_files"
            if ignore_system_reads and self._is_system_read(path):
                return "system_reads"
            if ignore_torch_cache and self._is_torch_cache(path):
                return "torch_cache"
            if ignore_library_caches and self._is_library_cache(path):
                return "library_caches"
            if ignore_package_reads and self._is_package_file(
                path, sys_prefix, sys_base_prefix, editable_dirs=editable_dirs
            ):
                return "package_reads"
            # Drop /tmp reads only when the job also wrote the file (an
            # endogenous intermediate). A read-only /tmp file pre-existed the
            # run and is kept as a (pre-existing) input.
            if ignore_tmp_files and self._is_tmp_path(path) and path in written_set:
                return "tmp_files"
            if self._matches_ignore_paths(path, ignore_abs, ignore_rel):
                return "ignore_paths"
            return None

        def filter_reads(paths: list[str]) -> list[str]:
            kept: list[str] = []
            for p in paths:
                cat = categorize_read(p)
                if cat is None:
                    kept.append(p)
                else:
                    _record_drop(cat, p)
            return kept

        opened_files = filter_reads(tracer_data.opened_files)
        read_files = filter_reads(tracer_data.read_files)
        modules_files = filter_reads(python_data.modules_files)
        self.logger.debug(
            "After read filtering: opened=%d->%d, read=%d->%d, modules=%d->%d",
            len(tracer_data.opened_files),
            len(opened_files),
            len(tracer_data.read_files),
            len(read_files),
            len(python_data.modules_files),
            len(modules_files),
        )

        # Filter writes
        read_files_set: set[str] = set(tracer_data.read_files)
        tmp_files_to_delete: list[str] = []
        filtered_written_files: list[str] = []

        for f in tracer_data.written_files:
            if roar_inject_dir and f.startswith(roar_inject_dir):
                _record_drop("roar_internal", f)
                continue
            if self._is_write_noise(f):
                _record_drop("write_noise", f)
                continue
            if self._is_credential_file(f):
                _record_drop("credential_files", f)
                continue
            if ignore_torch_cache and self._is_torch_cache(f):
                _record_drop("torch_cache", f)
                continue
            if ignore_library_caches and self._is_library_cache(f):
                _record_drop("library_caches", f)
                continue
            if self._is_tmp_path(f):
                if ignore_tmp_files:
                    _record_drop("tmp_files", f)
                    continue
                if f not in read_files_set and delete_tmp_writes:
                    tmp_files_to_delete.append(f)
            if self._matches_ignore_paths(f, ignore_abs, ignore_rel):
                _record_drop("ignore_paths", f)
                continue
            filtered_written_files.append(f)

        # Delete /tmp files if strict mode enabled
        deleted_count = self._cleanup_tmp_files(tmp_files_to_delete)
        self.logger.debug(
            "After write filtering: written=%d->%d, tmp_deleted=%d",
            len(tracer_data.written_files),
            len(filtered_written_files),
            deleted_count,
        )

        return FilteredFiles(
            read_files=read_files,
            written_files=filtered_written_files,
            opened_files=opened_files,
            modules_files=modules_files,
            tmp_files_deleted=deleted_count,
            counts=FilterCounts(**counts),
            dropped_paths=dropped,
        )

    @staticmethod
    def _is_tmp_path(path: str) -> bool:
        """Check if path is a temporary file (Linux /tmp/ or macOS /private/var/folders/)."""
        return path.startswith("/tmp/") or path.startswith("/private/var/folders/")

    def _is_system_read(self, path: str) -> bool:
        """Check if path is a system file read."""
        return path.startswith(self.SYSTEM_READ_PREFIXES)

    def _is_torch_cache(self, path: str) -> bool:
        """Check if path is a torch/triton cache file."""
        return any(path.startswith(pattern) for pattern in self.TORCH_CACHE_PATTERNS)

    def _is_library_cache(self, path: str) -> bool:
        """Check if path is in a curated well-known per-library user-cache subdir.

        Matched as a substring so any user-home variant (``~/.cache/``,
        ``/root/.cache/``, etc.) is caught without enumerating every home
        layout. Project-specific cache subdirs that aren't on the curated
        ``LIBRARY_CACHE_PATTERNS`` list stay tracked (e.g. ``~/.cache/<your
        project>/`` is intentionally NOT filtered).
        """
        return any(pattern in path for pattern in self.LIBRARY_CACHE_PATTERNS)

    @staticmethod
    def _is_roar_internal(path: str) -> bool:
        """Check if path is a roar-internal file/directory."""
        return (
            "/.roar/" in path
            or path.endswith("/.roar")
            or path.startswith(".roar/")
            or path == ".roar"
        )

    @staticmethod
    def _is_credential_file(path: str) -> bool:
        """Check if path is a credentials file that must never enter lineage.

        These hold secrets (not reproducibility-relevant data), so they are
        always filtered from inputs and outputs regardless of config — a user
        clearing ``filters.ignore_paths`` must not re-expose them.
        """
        basename = path.rsplit("/", 1)[-1]
        # .netrc / _netrc: machine login tokens (curl, requests, huggingface,
        # wandb). The _netrc spelling is the Windows / NETRC-env variant.
        return basename in (".netrc", "_netrc")

    @staticmethod
    def _is_git_metadata(path: str) -> bool:
        """Check if path is git metadata used for roar context capture."""
        if "/.git/" in path or path.endswith("/.git") or path.startswith(".git/") or path == ".git":
            return True
        if path.endswith("/.gitconfig") or path.endswith("/.gitmodules"):
            return True
        return path == ".gitconfig" or path == ".gitmodules"

    @staticmethod
    def _is_package_file(
        path: str,
        sys_prefix: str,
        sys_base_prefix: str,
        editable_dirs: frozenset[str] | None = None,
    ) -> bool:
        """Check if path is from an installed package."""
        # Check site-packages
        if "site-packages" in path:
            return True
        # Check if under sys_prefix (venv)
        if sys_prefix and path.startswith(str(Path(sys_prefix).resolve())):
            return True
        # Check stdlib (under sys_base_prefix but not site-packages)
        if sys_base_prefix:
            base = str(Path(sys_base_prefix).resolve())
            if path.startswith(base) and "site-packages" not in path:
                return True
        # Check editable installs (source roots from direct_url metadata)
        if editable_dirs:
            for editable_dir in editable_dirs:
                if path.startswith(editable_dir):
                    return True
        return False

    def _is_write_noise(self, path: str) -> bool:
        """Check if path is noise that shouldn't be in written_files."""
        if path.startswith(self.WRITE_NOISE_PREFIXES):
            return True
        # /etc writes are almost always noise (e.g., /etc/hosts lookup)
        if path.startswith("/etc/"):
            return True
        # roar's own files (log files in .roar/, etc.)
        if "/.roar/" in path or path.startswith(".roar/"):
            return True
        # Python bytecode cache files
        return bool(path.endswith(".pyc"))

    @staticmethod
    def _matches_ignore_paths(
        path: str,
        abs_prefixes: list[str],
        rel_prefixes: list[str],
    ) -> bool:
        """Check if path matches any user-configured ignore_paths pattern.

        Absolute patterns (starting with ``/``) are matched against the full
        path.  Relative patterns are matched as substrings — if the pattern
        appears anywhere in the path, it matches.  A trailing ``/`` on the
        pattern means "directory prefix"; otherwise the pattern is matched
        as a path component substring.
        """
        if any(path.startswith(prefix) for prefix in abs_prefixes):
            return True
        return any(pattern in path for pattern in rel_prefixes)

    def _cleanup_tmp_files(self, files: list[str]) -> int:
        """
        Delete temporary files if strict mode enabled.

        Args:
            files: List of /tmp files to delete

        Returns:
            Number of files successfully deleted
        """
        if not files:
            return 0

        deleted_count = 0
        for tmp_file in files:
            try:
                if os.path.exists(tmp_file):
                    os.remove(tmp_file)
                    deleted_count += 1
            except OSError:
                pass  # May not have permission, file in use, etc.

        if deleted_count > 0:
            self.logger.debug("Cleaned up %d /tmp file(s)", deleted_count)

        return deleted_count
