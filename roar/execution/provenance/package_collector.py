"""
Package collector service for provenance collection.

Collects package information from pip, dpkg, and other package managers.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from ...core.interfaces.logger import ILogger
from ...core.interfaces.provenance import PythonInjectData

_DPKG_CACHE_FILENAME = "dpkg_lib_cache.json"
_DPKG_STATUS_PATH = "/var/lib/dpkg/status"


class PackageCollectorService:
    """Collects package information from various package managers."""

    def __init__(self, logger: ILogger | None = None, cache_dir: str | None = None) -> None:
        """Initialize package collector with optional logger and cache directory."""
        self._logger = logger
        self._cache_dir = cache_dir

    @property
    def logger(self) -> ILogger:
        """Get logger, resolving from container or creating NullLogger."""
        if self._logger is None:
            from ...core.logging import get_logger

            self._logger = get_logger()
        return self._logger

    def collect(
        self,
        python_data: PythonInjectData,
        shared_libs: list[str],
        sys_prefix: str,
    ) -> dict[str, dict[str, str | None]]:
        """
        Collect package info organized by manager (pip, dpkg, etc.).

        Args:
            python_data: Python inject data with package info
            shared_libs: List of shared library paths
            sys_prefix: Python sys.prefix path

        Returns:
            Dict with manager names as keys and package dicts as values
        """
        self.logger.debug("PackageCollectorService.collect: shared_libs=%d", len(shared_libs))
        packages: dict[str, dict[str, str | None]] = {}

        # pip packages from used_packages
        if python_data.used_packages:
            packages["pip"] = dict(python_data.used_packages)  # type: ignore[assignment]
            self.logger.debug("Pip packages from used_packages: %d", len(packages["pip"]))

        # Extract dpkg packages from shared_libs
        self.logger.debug("Collecting dpkg packages from shared libs")
        dpkg_packages = self._collect_dpkg_packages(
            shared_libs, sys_prefix, python_data.installed_packages
        )
        if dpkg_packages:
            packages["dpkg"] = dpkg_packages
            self.logger.debug("Dpkg packages collected: %d", len(dpkg_packages))

        self.logger.debug("Package collection complete: managers=%s", list(packages.keys()))
        return packages

    def detect_package_manager(self, python_data: PythonInjectData) -> list[str]:
        """
        Detect which package manager(s) are in use based on child environment.

        Args:
            python_data: Python inject data

        Returns:
            List of detected package manager names
        """
        self.logger.debug("Detecting package manager for sys_prefix=%s", python_data.sys_prefix)
        managers = []
        venv_path = python_data.sys_prefix

        if not venv_path:
            self.logger.debug("No sys_prefix, using system manager")
            return ["system"]

        pyvenv_cfg = Path(venv_path) / "pyvenv.cfg"
        if pyvenv_cfg.exists():
            try:
                content = pyvenv_cfg.read_text().lower()
                if "uv" in content:
                    managers.append("uv")
                else:
                    managers.append("pip")
            except Exception as e:
                self.logger.debug("Failed to read pyvenv.cfg: %s", e)
                managers.append("pip")

        # Check for conda
        conda_meta = Path(venv_path) / "conda-meta"
        if conda_meta.exists():
            managers.append("conda")

        result = managers if managers else ["unknown"]
        self.logger.debug("Detected package managers: %s", result)
        return result

    def _collect_dpkg_packages(
        self,
        shared_libs: list[str],
        sys_prefix: str,
        installed_packages: dict[str, str],
    ) -> dict[str, str | None]:
        """
        Collect dpkg package info from shared libraries.

        Args:
            shared_libs: List of shared library paths
            sys_prefix: Python sys.prefix path
            installed_packages: Dict of installed pip packages

        Returns:
            Dict of dpkg package names to versions
        """
        if sys.platform != "linux":
            return {}

        # Get shared lib info and find dpkg-managed ones
        dpkg_pkg_names: set[str] = set()
        libs_info = self._get_shared_libs_info(shared_libs, sys_prefix, installed_packages)
        self.logger.debug("Classified %d shared libraries", len(libs_info))

        for lib in libs_info:
            if lib.get("manager") == "dpkg" and lib.get("package"):
                dpkg_pkg_names.add(lib["package"])

        if not dpkg_pkg_names:
            self.logger.debug("No dpkg-managed libraries found")
            return {}

        # Batch query dpkg for versions (one subprocess call)
        self.logger.debug("Querying dpkg for %d packages", len(dpkg_pkg_names))
        dpkg_packages: dict[str, str | None] = {}
        try:
            result = subprocess.run(
                ["dpkg-query", "-W", "-f", "${Package}\t${Version}\n", *list(dpkg_pkg_names)],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                self.logger.debug("dpkg-query succeeded")
                for line in result.stdout.strip().split("\n"):
                    if "\t" in line:
                        pkg, version = line.split("\t", 1)
                        dpkg_packages[pkg] = version
        except Exception as e:
            # Fall back to no versions
            self.logger.debug("dpkg-query failed, falling back to no versions: %s", e)
            for pkg in dpkg_pkg_names:
                dpkg_packages[pkg] = None

        return dpkg_packages

    def _get_shared_libs_info(
        self,
        shared_libs: list[str],
        sys_prefix: str,
        installed_packages: dict[str, str],
    ) -> list[dict[str, Any]]:
        """
        Get information about shared libraries including their source.

        Classifies each library as pip-managed, venv-managed, or dpkg-managed.
        The dpkg lookups use a persistent cache (keyed by dpkg database mtime)
        and a single batched ``dpkg -S`` call for any cache misses.

        Args:
            shared_libs: List of shared library paths
            sys_prefix: Python sys.prefix path
            installed_packages: Dict of installed pip packages

        Returns:
            List of dicts with path, managed, manager, package info
        """
        libs_info: list[dict[str, Any]] = []
        needs_dpkg: list[int] = []  # indices into libs_info needing dpkg lookup
        resolved_prefix = Path(sys_prefix).resolve() if sys_prefix else None

        for lib_path in shared_libs:
            if lib_path.startswith("/proc/") or lib_path.startswith("/dev/"):
                continue

            info: dict[str, Any] = {"path": lib_path}

            # Check if it's in site-packages (pip-managed)
            if "site-packages" in lib_path:
                info["managed"] = True
                info["manager"] = "pip"
                pkg_name = self._extract_package_from_site_packages(lib_path)
                if pkg_name:
                    pkg_name_normalized = pkg_name.lower().replace("-", "_")
                    for installed_name in installed_packages:
                        if installed_name.lower().replace("-", "_") == pkg_name_normalized:
                            info["package"] = installed_name
                            break
                    else:
                        info["package"] = pkg_name
                libs_info.append(info)
                continue

            # Check if under sys_prefix (venv)
            if resolved_prefix:
                try:
                    Path(lib_path).relative_to(resolved_prefix)
                    info["managed"] = True
                    info["manager"] = "pip"
                    libs_info.append(info)
                    continue
                except ValueError:
                    pass

            # Needs dpkg lookup — collect for batch resolution
            libs_info.append(info)
            needs_dpkg.append(len(libs_info) - 1)

        # Resolve dpkg ownership via cache + single batched subprocess
        if needs_dpkg:
            dpkg_paths = [libs_info[i]["path"] for i in needs_dpkg]
            dpkg_results = self._resolve_dpkg_owners(dpkg_paths)
            for idx in needs_dpkg:
                lib_path = libs_info[idx]["path"]
                pkg_name = dpkg_results.get(lib_path)
                if pkg_name:
                    libs_info[idx]["package"] = pkg_name
                    libs_info[idx]["managed"] = True
                    libs_info[idx]["manager"] = "dpkg"
                else:
                    libs_info[idx]["managed"] = False

        return libs_info

    # ------------------------------------------------------------------
    # dpkg file→package cache (invalidated by dpkg database mtime)
    # ------------------------------------------------------------------

    @staticmethod
    def _dpkg_db_mtime() -> float:
        """Return mtime of the dpkg status database, or 0 if unavailable."""
        try:
            return os.stat(_DPKG_STATUS_PATH).st_mtime
        except OSError:
            return 0.0

    def _load_dpkg_cache(self) -> tuple[float, dict[str, str]]:
        """Load cached lib_path→package mappings. Returns (mtime, mapping)."""
        if not self._cache_dir:
            return 0.0, {}
        try:
            with open(os.path.join(self._cache_dir, _DPKG_CACHE_FILENAME)) as f:
                data = json.load(f)
            return float(data.get("dpkg_mtime", 0)), data.get("libs", {})
        except (OSError, json.JSONDecodeError, ValueError):
            return 0.0, {}

    def _save_dpkg_cache(self, dpkg_mtime: float, libs: dict[str, str]) -> None:
        """Persist lib_path→package mappings."""
        if not self._cache_dir:
            return
        try:
            with open(os.path.join(self._cache_dir, _DPKG_CACHE_FILENAME), "w") as f:
                json.dump({"dpkg_mtime": dpkg_mtime, "libs": libs}, f)
        except OSError:
            pass

    def _resolve_dpkg_owners(self, lib_paths: list[str]) -> dict[str, str]:
        """
        Map library paths to owning dpkg package names.

        Uses a persistent cache keyed on ``/var/lib/dpkg/status`` mtime.
        Only runs ``dpkg -S`` for paths not already in the cache.

        Args:
            lib_paths: Library file paths to look up

        Returns:
            Dict mapping path → package name for dpkg-owned paths
        """
        current_mtime = self._dpkg_db_mtime()
        cached_mtime, cached_libs = self._load_dpkg_cache()

        # Invalidate cache if dpkg database changed
        if cached_mtime != current_mtime:
            self.logger.debug("dpkg cache miss (db mtime changed)")
            cached_libs = {}
        else:
            self.logger.debug("dpkg cache valid, %d entries", len(cached_libs))

        results: dict[str, str] = {}
        uncached: list[str] = []

        for path in lib_paths:
            if path in cached_libs:
                results[path] = cached_libs[path]
            else:
                uncached.append(path)

        # Batch dpkg -S for all uncached paths in one subprocess call
        if uncached:
            self.logger.debug("Running dpkg -S for %d uncached libs", len(uncached))
            try:
                result = subprocess.run(
                    ["dpkg", "-S", *uncached],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                # dpkg -S outputs "pkg:arch: /path" lines.
                # Non-zero exit is expected when some paths aren't found,
                # but matches still appear on stdout.
                for line in result.stdout.strip().split("\n"):
                    if ": " in line:
                        pkg_part, path_part = line.split(": ", 1)
                        pkg_name = pkg_part.split(":")[0].strip()
                        path = path_part.strip()
                        results[path] = pkg_name
                        cached_libs[path] = pkg_name
            except Exception as e:
                self.logger.debug("Batch dpkg -S failed: %s", e)

            # Persist updated cache
            self._save_dpkg_cache(current_mtime, cached_libs)

        return results

    def _extract_package_from_site_packages(self, lib_path: str) -> str | None:
        """
        Extract package name from a site-packages path.

        E.g., /path/site-packages/numpy/core/_multiarray.so -> numpy
              /path/site-packages/nvidia/cudnn/lib/libcudnn.so -> nvidia-cudnn (heuristic)

        Args:
            lib_path: Path to a library in site-packages

        Returns:
            Package name or None
        """
        if "site-packages/" not in lib_path:
            return None

        # Get the part after site-packages/
        idx = lib_path.find("site-packages/")
        after_sp = lib_path[idx + len("site-packages/") :]
        parts = after_sp.split("/")

        if not parts:
            return None

        top_dir = parts[0]

        # Handle nvidia namespace packages specially
        if top_dir == "nvidia" and len(parts) > 1:
            # e.g., nvidia/cudnn/lib/... -> nvidia-cudnn-cu12 (approximate)
            sub_pkg = parts[1]
            return f"nvidia-{sub_pkg}"

        # Handle .libs directories (e.g., numpy.libs)
        if top_dir.endswith(".libs") and len(parts) >= 1:
            return top_dir[:-5]  # Remove .libs suffix

        return top_dir
