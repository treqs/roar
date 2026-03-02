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

    # ------------------------------------------------------------------ #
    # Package-map disk cache                                              #
    # ------------------------------------------------------------------ #
    # Installed packages rarely change between runs.  We cache the
    # {pkg_versions, pkg_dist_map} dicts in a JSON file under
    # ~/.cache/roar/ and invalidate on site-packages mtime changes.
    # Cache miss: ~190ms full scan.  Cache hit: ~5ms JSON load.
    # ------------------------------------------------------------------ #

    _CACHE_VERSION = 2  # bump when cache format changes

    def _pkg_map_cache_path(self) -> "Path | None":
        """Return path for the package-map cache file, or None if uncacheable."""
        try:
            cache_dir = Path.home() / ".cache" / "roar"
            cache_dir.mkdir(parents=True, exist_ok=True)
            return cache_dir / "pkg_map.json"
        except OSError:
            return None

    def _pkg_map_cache_key(self) -> str:
        """A cache-invalidation key derived from site-packages directory mtimes."""
        mtimes: list[str] = []
        for sp_dir in self._site_packages_dirs:
            try:
                mtime = os.stat(sp_dir).st_mtime
                mtimes.append(f"{sp_dir}:{mtime:.0f}")
            except OSError:
                mtimes.append(f"{sp_dir}:missing")
        # Also include Python version so we invalidate on interpreter upgrades.
        mtimes.append(sys.version)
        return "|".join(mtimes)

    def _load_pkg_map_cache(
        self, cache_path: "Path", cache_key: str
    ) -> "tuple[dict, dict] | None":
        """Load cached (pkg_versions, pkg_dist_map) if cache is valid, else None."""
        import json

        try:
            data = json.loads(cache_path.read_text())
        except (OSError, ValueError):
            return None

        if data.get("version") != self._CACHE_VERSION:
            return None
        if data.get("key") != cache_key:
            return None

        pkg_versions = data.get("pkg_versions")
        pkg_dist_map = data.get("pkg_dist_map")
        if not isinstance(pkg_versions, dict) or not isinstance(pkg_dist_map, dict):
            return None

        return pkg_versions, pkg_dist_map

    def _save_pkg_map_cache(
        self,
        cache_path: "Path",
        cache_key: str,
        pkg_versions: dict,
        pkg_dist_map: dict,
    ) -> None:
        """Write (pkg_versions, pkg_dist_map) to the disk cache atomically."""
        import json

        data = {
            "version": self._CACHE_VERSION,
            "key": cache_key,
            "pkg_versions": pkg_versions,
            "pkg_dist_map": pkg_dist_map,
        }
        tmp = cache_path.with_suffix(".tmp")
        from contextlib import suppress

        try:
            tmp.write_text(json.dumps(data, separators=(",", ":")))
            tmp.replace(cache_path)
        except OSError:
            with suppress(OSError):
                tmp.unlink(missing_ok=True)

    def _build_package_file_map(self) -> tuple[dict, dict]:
        """Build package version map and a fast module-name index.

        Single-pass over distributions() reading only METADATA + top_level.txt.
        Never reads RECORD / dist.files() — those are large and slow.

        Previously (v1): iterated dist.files() for every package — O(packages x
        files_per_package) disk I/O, ~9 seconds with 312 packages.

        Previously (v2): called packages_distributions() which internally calls
        dist.files() via _top_level_inferred() for the ~42 packages that lack
        top_level.txt — still ~0.47 seconds and a second distributions() sweep.

        v3 (current): single sweep reading METADATA + top_level.txt only, plus a
        disk cache keyed on site-packages mtime.  Cache hit: ~5ms.  Cache miss
        (first run or after package install): ~190ms then cached.
        """
        # Try disk cache first.
        cache_path = self._pkg_map_cache_path()
        cache_key = self._pkg_map_cache_key()
        pkg_versions: dict[str, str]
        pkg_dist_map: dict[str, list[str]]
        if cache_path is not None:
            cached = self._load_pkg_map_cache(cache_path, cache_key)
            if cached is not None:
                pkg_versions, pkg_dist_map = cached
                self._pkg_dist_map: dict[str, list[str]] = pkg_dist_map
                return {}, pkg_versions

        # Cache miss — full scan.
        from importlib.metadata import distributions

        pkg_versions = {}
        pkg_dist_map = {}

        for dist in distributions():
            try:
                name = dist.metadata["Name"]
                version = dist.metadata["Version"]
            except (KeyError, TypeError):
                continue
            if not name or not version:
                continue
            pkg_versions[name] = version

            # top_level.txt is a tiny text file listing top-level import names.
            # If it exists, use it.  If not, skip — do NOT call dist.files().
            top_level_text = dist.read_text("top_level.txt")
            if top_level_text:
                for module in top_level_text.split():
                    module = module.strip()
                    if module:
                        pkg_dist_map.setdefault(module, []).append(name)

        self._pkg_dist_map = pkg_dist_map

        # Persist cache for next run.
        if cache_path is not None:
            self._save_pkg_map_cache(cache_path, cache_key, pkg_versions, pkg_dist_map)

        # file_to_pkg is intentionally empty; classify() uses path extraction.
        return {}, pkg_versions

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

        # Check if it's a package file via fast path-based lookup.
        # _file_to_pkg is always empty now; we extract the top-level module name
        # directly from the path and look it up in packages_distributions() index.
        if "site-packages" in path_str:
            for sp_dir in self._site_packages_dirs:
                if path_str.startswith(sp_dir + os.sep) or path_str == sp_dir:
                    sp_rel = path_str[len(sp_dir):].lstrip(os.sep)
                    top = sp_rel.split(os.sep)[0]
                    if top.endswith(".py"):
                        top = top[:-3]
                    if top and not top.endswith(".dist-info") and not top.endswith(".egg-info"):
                        pkg_names = getattr(self, "_pkg_dist_map", {}).get(top, [])
                        if pkg_names:
                            return ("package", pkg_names[0])
                    return ("package", "unknown")
            # site-packages in path but not under a tracked site-packages dir
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
        """Check if a file is a system shared library (.so/.dylib file in system paths).

        This is checked early to prevent misclassification when sys.prefix = /usr.
        """
        # Check for .so/.dylib files in known system library directories
        system_lib_dirs = [
            "/usr/lib",
            "/lib",
            "/usr/lib64",
            "/lib64",
            "/usr/local/lib",
            # macOS
            "/System/Library/Frameworks",
        ]
        if ".so" in path_str or path_str.endswith(".dylib"):
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
            # macOS
            "/System/Library",
            "/Library/Frameworks",
        ]

        for prefix in system_prefixes:
            if path_str.startswith(prefix):
                return True

        # Also check for .so/.dylib files anywhere (shared libraries)
        if ".so" in path_str and ("/lib" in path_str or "/usr" in path_str):
            return True
        return bool(path_str.endswith(".dylib") and ("/lib" in path_str or "/Library" in path_str))

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
