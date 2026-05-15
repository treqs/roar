"""Generic runtime-activity tracking for injected Python processes."""

from __future__ import annotations

import builtins
import contextlib
import json
import os
import platform
import sys
from collections.abc import Mapping, MutableMapping, Sequence
from typing import Any, Protocol, cast

from roar.execution.runtime.inject.support import is_suppressed

_ORIGINAL_ENVIRON_GET_ATTR = "_original_get"
_ENVIRON_GET_METHOD_NAME = "get"


class RuntimeImportObserver(Protocol):
    """Minimal protocol for the runtime import controller."""

    def handle_import(self, module_name: str, module: Any) -> Any:
        """Observe a module import."""


def get_loaded_shared_libs(real_open) -> list[str]:
    libs: set[str] = set()
    try:
        with real_open("/proc/self/maps", "r") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) < 6:
                    continue
                path = parts[5]
                if path.endswith(".so") or ".so." in path:
                    libs.add(path)
    except Exception:
        pass
    return sorted(libs)


def get_installed_packages() -> dict[str, str]:
    packages: dict[str, str] = {}
    try:
        from importlib import metadata as importlib_metadata

        for dist in importlib_metadata.distributions():
            metadata = cast(Mapping[str, str], dist.metadata)
            name = metadata.get("Name", None)
            version = metadata.get("Version", None)
            if name and version:
                packages[name] = version
    except Exception:
        pass
    return packages


def get_used_packages(
    modules_files: Sequence[str],
    installed_packages: Mapping[str, str | None],
) -> dict[str, str | None]:
    used: dict[str, str | None] = {}

    try:
        from importlib import metadata as importlib_metadata

        pkg_dist_map = importlib_metadata.packages_distributions()
    except Exception:
        pkg_dist_map = {}

    try:
        for fpath in modules_files:
            if "site-packages" not in fpath:
                continue
            idx = fpath.find("site-packages/")
            if idx < 0:
                continue
            after_sp = fpath[idx + len("site-packages/") :]
            top_dir = after_sp.split("/")[0]
            if top_dir.endswith(".py"):
                top_dir = top_dir[:-3]
            if top_dir.endswith(".dist-info") or top_dir.endswith(".egg-info"):
                continue
            if top_dir.startswith("_") or top_dir.endswith(".so"):
                continue

            pkg_names = pkg_dist_map.get(top_dir, [])
            for pkg_name in pkg_names:
                if pkg_name in installed_packages and pkg_name not in used:
                    used[pkg_name] = installed_packages[pkg_name]

            if not pkg_names and top_dir not in used:
                used[top_dir] = None
    except Exception:
        pass

    return used


def get_active_runtime_pythonpath(environ: Mapping[str, str]) -> tuple[str, ...]:
    entries: list[str] = []
    for raw_path in environ.get("ROAR_RUNTIME_PYTHONPATH_ACTIVE", "").split(os.pathsep):
        if not raw_path:
            continue
        with contextlib.suppress(Exception):
            entries.append(os.path.abspath(raw_path).rstrip(os.sep) + os.sep)
    return tuple(entries)


def is_under_any_runtime_path(path: str, runtime_paths: Sequence[str]) -> bool:
    if not runtime_paths:
        return False
    abs_path = os.path.abspath(path)
    return any(abs_path.startswith(runtime_path) for runtime_path in runtime_paths)


class RuntimeInjectionTracker:
    """Capture generic process activity for the parent-side recorder."""

    def __init__(
        self,
        environ: MutableMapping[str, str],
        runtime_import_controller: RuntimeImportObserver,
        *,
        log_file: str | None = None,
        inject_dir: str | None = None,
    ) -> None:
        self._environ = environ
        self._runtime_import_controller = runtime_import_controller
        self._real_open = builtins.open
        self._real_import = builtins.__import__
        self._original_environ_get = environ.get
        self._log_file = log_file if log_file is not None else environ.get("ROAR_LOG_FILE")
        self._inject_dir = inject_dir or os.path.dirname(os.path.abspath(__file__))
        self.opened_files: set[str] = set()
        self.imported_modules: set[str] = set()
        self.env_reads: dict[str, str] = {}

        if not hasattr(self._environ, _ORIGINAL_ENVIRON_GET_ATTR):
            with contextlib.suppress(Exception):
                setattr(self._environ, _ORIGINAL_ENVIRON_GET_ATTR, self._environ.get)
        else:
            self._original_environ_get = getattr(self._environ, _ORIGINAL_ENVIRON_GET_ATTR)

    def install(self) -> None:
        """Patch builtins and environ access for activity capture."""
        builtins.open = self.tracking_open
        builtins.__import__ = self.tracking_import
        setattr(self._environ, _ENVIRON_GET_METHOD_NAME, self.patched_environ_get)

    def tracking_open(self, *args, **kwargs):
        if is_suppressed():
            return self._real_open(*args, **kwargs)
        try:
            path = args[0]
            self.opened_files.add(os.path.abspath(path))
        except Exception:
            pass
        return self._real_open(*args, **kwargs)

    def tracking_import(self, name, globals=None, locals=None, fromlist=(), level=0):
        self.imported_modules.add(name)
        module = self._real_import(name, globals, locals, fromlist, level)

        if self._environ.get("ROAR_WRAP") != "1":
            return module

        self._runtime_import_controller.handle_import(name, module)
        return module

    def patched_environ_get(self, key, default=None):
        if key in self._environ:
            self.env_reads[key] = self._environ[key]
        return self._original_environ_get(key, default)

    def write_log(self) -> None:
        if not self._log_file:
            return

        runtime_pythonpath = get_active_runtime_pythonpath(self._environ)
        modules_files = sorted(
            os.path.abspath(getattr(module, "__file__", ""))
            for module in sys.modules.values()
            if getattr(module, "__file__", None)
            and not os.path.abspath(getattr(module, "__file__", "")).startswith(self._inject_dir)
            and not is_under_any_runtime_path(
                os.path.abspath(getattr(module, "__file__", "")),
                runtime_pythonpath,
            )
        )
        installed_packages = get_installed_packages()
        used_packages = get_used_packages(modules_files, installed_packages)
        data = {
            "opened_files": sorted(self.opened_files),
            "imported_modules": sorted(self.imported_modules),
            "env_reads": dict(sorted(self.env_reads.items())),
            "modules_files": modules_files,
            "roar_inject_dir": self._inject_dir,
            "shared_libs": get_loaded_shared_libs(self._real_open),
            "sys_prefix": sys.prefix,
            "sys_base_prefix": sys.base_prefix,
            "virtual_env": self._original_environ_get("VIRTUAL_ENV", ""),
            "argv": sys.argv,
            "installed_packages": installed_packages,
            "used_packages": used_packages,
            "python_version": platform.python_version(),
            "python_implementation": platform.python_implementation(),
        }
        with self._real_open(self._log_file, "w") as handle:
            json.dump(data, handle)


__all__ = [
    "RuntimeInjectionTracker",
    "get_installed_packages",
    "get_loaded_shared_libs",
    "get_used_packages",
]
