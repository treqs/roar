"""File classification filter for ptrace data."""

import os
import subprocess
import sys
from pathlib import Path


class FileClassifier:
    """
    Classifies files into categories: repo, package, stdlib, system, unmanaged, etc.

    This is the main filter for transforming raw file lists from the tracer
    into categorized provenance data.
    """

    def __init__(
        self,
        repo_root: str,
        sys_prefix: str | None = None,
        sys_base_prefix: str | None = None,
        roar_inject_dir: str = "",
    ):
        self.repo_root = Path(repo_root).resolve()
        self.sys_prefix = Path(sys_prefix or sys.prefix).resolve()
        self.sys_base_prefix = Path(sys_base_prefix or sys.base_prefix).resolve()
        self.roar_inject_dir = roar_inject_dir

        # Build package maps
        self._site_packages_dirs = self._get_site_packages_dirs()
        self._file_to_pkg, self._pkg_versions = self._build_package_file_map()

    def _get_site_packages_dirs(self) -> list[str]:
        """Get possible site-packages directories for the current prefix."""
        dirs = []
        for subdir in ["lib", "lib64"]:
            for pyver in [
                f"python{sys.version_info.major}.{sys.version_info.minor}",
                "python3",
                "python",
            ]:
                sp = self.sys_prefix / subdir / pyver / "site-packages"
                if sp.exists():
                    dirs.append(str(sp.resolve()))
        return dirs

    def _build_package_file_map(self) -> tuple[dict, dict]:
        """Build a map of file paths to package names using importlib.metadata."""
        from importlib.metadata import distributions

        file_to_pkg = {}
        pkg_versions = {}

        for dist in distributions():
            name = dist.metadata["Name"]
            version = dist.metadata["Version"]
            pkg_versions[name] = version

            if dist.files:
                for f in dist.files:
                    try:
                        full_path = str(Path(str(dist.locate_file(f))).resolve())
                        file_to_pkg[full_path] = name
                    except Exception:
                        pass

        return file_to_pkg, pkg_versions

    def classify(self, path: str) -> tuple[str, str | None]:
        """
        Classify a file into one of:
        - "repo": tracked in the git repo
        - "package": from an installed Python package (returns package name)
        - "stdlib": Python standard library
        - "system": system file (OS libraries, config, etc.)
        - "unmanaged": not tracked anywhere
        - "external": /dev/*, /proc/*
        - "skip": should be skipped (doesn't exist, roar inject dir, etc.)

        Returns:
            Tuple of (classification, package_name_or_none)
        """
        path_str = str(Path(path).resolve())

        # Skip non-existent files
        if not os.path.exists(path_str):
            return ("skip", None)

        # Skip roar's inject directory
        if self.roar_inject_dir and path_str.startswith(self.roar_inject_dir):
            return ("skip", None)

        # External file (e.g., /dev/null, /proc/*)
        if path_str.startswith("/dev/") or path_str.startswith("/proc/"):
            return ("external", None)

        # Check if it's in the repo first (highest priority for user code)
        try:
            Path(path_str).relative_to(self.repo_root)
            # It's in the repo - but skip virtual environments inside the repo
            if ".venv" in path_str or "site-packages" in path_str:
                pass  # Fall through to package check
            else:
                try:
                    rel = Path(path_str).relative_to(self.repo_root)
                    subprocess.check_output(
                        ["git", "ls-files", "--error-unmatch", str(rel)],
                        cwd=str(self.repo_root),
                        stderr=subprocess.DEVNULL,
                    )
                    return ("repo", None)
                except subprocess.CalledProcessError:
                    # In repo but not tracked - could be generated file
                    return ("unmanaged", None)
        except ValueError:
            pass

        # Check if it's a package file (from importlib.metadata)
        if path_str in self._file_to_pkg:
            return ("package", self._file_to_pkg[path_str])

        # Check if it's in site-packages (even if not in metadata, it's a package)
        if "site-packages" in path_str:
            return ("package", "unknown")

        # Check for system shared libraries BEFORE sys.prefix check
        # This handles cases where sys.prefix = /usr (system Python)
        # and system .so files would otherwise be misclassified
        if self._is_system_shared_lib(path_str):
            return ("system", None)

        # Check if it's stdlib (before sys.prefix check, since sys.prefix may equal
        # sys.base_prefix in system Python installations)
        if self._is_stdlib_file(path_str):
            return ("stdlib", None)

        # Check if it's under sys.prefix (the virtual environment) - it's a package
        try:
            Path(path_str).relative_to(self.sys_prefix)
            return ("package", "unknown")
        except ValueError:
            pass

        # Check if it's a system file
        if self._is_system_file(path_str):
            return ("system", None)

        return ("unmanaged", None)

    def _is_stdlib_file(self, path: str) -> bool:
        """Check if a file is part of the Python standard library."""
        path_obj = Path(path).resolve()
        try:
            path_obj.relative_to(self.sys_base_prefix)
            # It's under the base prefix - check it's not in site-packages
            if "site-packages" not in str(path):
                return True
        except ValueError:
            pass
        return False

    def _is_system_shared_lib(self, path_str: str) -> bool:
        """Check if a file is a system shared library (.so file in system paths).

        This is checked early to prevent misclassification when sys.prefix = /usr.
        """
        # Check for .so files in known system library directories
        system_lib_dirs = ["/usr/lib", "/lib", "/usr/lib64", "/lib64", "/usr/local/lib"]
        if ".so" in path_str:
            for lib_dir in system_lib_dirs:
                if path_str.startswith(lib_dir):
                    return True
        return False

    def _is_system_file(self, path_str: str) -> bool:
        """Check if a file is a system-managed file (OS packages, shared libs, etc.)."""
        system_prefixes = [
            "/usr/lib",
            "/usr/lib64",
            "/lib",
            "/lib64",
            "/usr/share",
            "/etc",
            "/usr/local/lib",
            "/opt",
        ]

        for prefix in system_prefixes:
            if path_str.startswith(prefix):
                return True

        # Also check for .so files anywhere (shared libraries)
        return bool(".so" in path_str and ("/lib" in path_str or "/usr" in path_str))

    def classify_all(self, paths: list[str]) -> dict:
        """
        Classify a list of file paths.

        Returns:
            Dict with keys:
                - repo_files: list of paths tracked in git
                - packages: dict of package_name -> version
                - unmanaged: list of unmanaged paths
                - stats: classification counts
        """
        repo_files = []
        used_packages = set()
        unmanaged = []
        stats = {
            "repo": 0,
            "package": 0,
            "stdlib": 0,
            "system": 0,
            "unmanaged": 0,
            "external": 0,
            "skip": 0,
        }

        for path in paths:
            if not path:
                continue
            classification, pkg_name = self.classify(path)
            stats[classification] = stats.get(classification, 0) + 1

            if classification == "repo":
                repo_files.append(path)
            elif classification == "package" and pkg_name and pkg_name != "unknown":
                used_packages.add(pkg_name)
            elif classification == "unmanaged":
                unmanaged.append(path)

        packages = {pkg: self._pkg_versions.get(pkg, "unknown") for pkg in sorted(used_packages)}

        return {
            "repo_files": sorted(repo_files),
            "packages": packages,
            "unmanaged": sorted(unmanaged),
            "stats": stats,
        }

    def get_package_versions(self) -> dict:
        """Get all known package versions."""
        return self._pkg_versions.copy()
