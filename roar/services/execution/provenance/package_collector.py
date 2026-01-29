"""
Package collector service for provenance collection.

Collects package information from pip, dpkg, and other package managers.
"""

import subprocess
from pathlib import Path
from typing import Any

from ....core.interfaces.logger import ILogger
from ....core.interfaces.provenance import PythonInjectData


class PackageCollectorService:
    """Collects package information from various package managers."""

    def __init__(self, logger: ILogger | None = None) -> None:
        """Initialize package collector with optional logger."""
        self._logger = logger

    @property
    def logger(self) -> ILogger:
        """Get logger, resolving from container or creating NullLogger."""
        if self._logger is None:
            from ....core.container import get_container
            from ....services.logging import NullLogger

            container = get_container()
            self._logger = container.try_resolve(ILogger)  # type: ignore[type-abstract]
            if self._logger is None:
                self._logger = NullLogger()
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

        Args:
            shared_libs: List of shared library paths
            sys_prefix: Python sys.prefix path
            installed_packages: Dict of installed pip packages

        Returns:
            List of dicts with path, managed, manager, package info
        """
        libs_info = []

        for lib_path in shared_libs:
            if lib_path.startswith("/proc/") or lib_path.startswith("/dev/"):
                continue
            libs_info.append(self._classify_shared_lib(lib_path, sys_prefix, installed_packages))

        return libs_info

    def _classify_shared_lib(
        self,
        lib_path: str,
        sys_prefix: str,
        installed_packages: dict[str, str],
    ) -> dict[str, Any]:
        """
        Classify a shared library - is it from a deb package, pip/uv package, or unmanaged.

        Args:
            lib_path: Path to the shared library
            sys_prefix: Python sys.prefix path
            installed_packages: Dict of installed pip packages

        Returns:
            Dict with path, managed, manager, package info
        """
        info: dict[str, Any] = {"path": lib_path}

        # Check if it's in site-packages (pip-managed)
        if "site-packages" in lib_path:
            info["managed"] = True
            info["manager"] = "pip"
            pkg_name = self._extract_package_from_site_packages(lib_path)
            if pkg_name:
                # Try to find matching installed package (case-insensitive, handle - vs _)
                pkg_name_normalized = pkg_name.lower().replace("-", "_")
                for installed_name in installed_packages:
                    if installed_name.lower().replace("-", "_") == pkg_name_normalized:
                        info["package"] = installed_name
                        break
                else:
                    # If exact match not found, use the extracted name
                    info["package"] = pkg_name
            return info

        # Check if under sys_prefix (venv)
        if sys_prefix:
            try:
                Path(lib_path).relative_to(Path(sys_prefix).resolve())
                info["managed"] = True
                info["manager"] = "pip"
                return info
            except ValueError:
                pass

        # Check if it's a system library (dpkg-managed)
        try:
            result = subprocess.run(
                ["dpkg", "-S", lib_path],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if result.returncode == 0:
                pkg_name = result.stdout.split(":")[0].strip()
                info["package"] = pkg_name
                info["managed"] = True
                info["manager"] = "dpkg"
            else:
                info["managed"] = False
        except Exception as e:
            self.logger.debug("dpkg -S failed for %s: %s", lib_path, e)
            info["managed"] = None

        return info

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
