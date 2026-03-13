import atexit
import builtins
import json
import os
import sys

from roar.execution.framework.runtime_imports import RuntimeImportController
from roar.execution.runtime.inject.support import is_suppressed

# ------------------------------------------------------------------------------
# Data structures the parent will ingest
# ------------------------------------------------------------------------------

opened_files = set()
imported_modules = set()
env_reads = {}
_ORIGINAL_ENVIRON_GET_ATTR = "_original_get"
_ENVIRON_GET_METHOD_NAME = "get"

# File where parent told us to write logs
LOG_FILE = os.environ.get("ROAR_LOG_FILE")

# Directory where this sitecustomize.py lives (to exclude from tracking)
_ROAR_INJECT_DIR = os.path.dirname(os.path.abspath(__file__))
_runtime_import_controller = RuntimeImportController(os.environ)

# ------------------------------------------------------------------------------
# Track open() calls
# ------------------------------------------------------------------------------
_real_open = builtins.open


def tracking_open(*args, **kwargs):
    if is_suppressed():
        return _real_open(*args, **kwargs)
    try:
        path = args[0]
        opened_files.add(os.path.abspath(path))
    except Exception:
        pass
    return _real_open(*args, **kwargs)


builtins.open = tracking_open

# ------------------------------------------------------------------------------
# Track imports and delegate runtime lifecycle to the framework controller
# ------------------------------------------------------------------------------
_real_import = builtins.__import__


def tracking_import(name, globals=None, locals=None, fromlist=(), level=0):
    imported_modules.add(name)
    module = _real_import(name, globals, locals, fromlist, level)

    if os.environ.get("ROAR_WRAP") != "1":
        return module

    _runtime_import_controller.handle_import(name, module)
    return module


builtins.__import__ = tracking_import

# ------------------------------------------------------------------------------
# Track environment variable reads
# ------------------------------------------------------------------------------

if not hasattr(os.environ, _ORIGINAL_ENVIRON_GET_ATTR):
    setattr(os.environ, _ORIGINAL_ENVIRON_GET_ATTR, os.environ.get)


def patched_environ_get(key, default=None):
    if key in os.environ:
        env_reads[key] = os.environ[key]
    original_get = getattr(os.environ, _ORIGINAL_ENVIRON_GET_ATTR)
    return original_get(key, default)


setattr(os.environ, _ENVIRON_GET_METHOD_NAME, patched_environ_get)


# ------------------------------------------------------------------------------
# On exit, write the record to LOG_FILE
# ------------------------------------------------------------------------------


def _get_loaded_shared_libs():
    libs = set()
    try:
        with _real_open("/proc/self/maps", "r") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 6:
                    path = parts[5]
                    if path.endswith(".so") or ".so." in path:
                        libs.add(path)
    except Exception:
        pass
    return sorted(libs)


def _get_installed_packages():
    packages = {}
    try:
        from importlib import metadata as importlib_metadata

        for dist in importlib_metadata.distributions():
            name = dist.metadata.get("Name")
            version = dist.metadata.get("Version")
            if name and version:
                packages[name] = version
    except Exception:
        pass
    return packages


def _get_used_packages(modules_files, installed_packages):
    used = {}

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


def _write_log():
    if not LOG_FILE:
        return

    modules_files = sorted(
        os.path.abspath(getattr(m, "__file__", ""))
        for m in sys.modules.values()
        if getattr(m, "__file__", None)
        and not os.path.abspath(getattr(m, "__file__", "")).startswith(_ROAR_INJECT_DIR)
    )

    installed_packages = _get_installed_packages()
    used_packages = _get_used_packages(modules_files, installed_packages)

    data = {
        "opened_files": sorted(opened_files),
        "imported_modules": sorted(imported_modules),
        "env_reads": dict(sorted(env_reads.items())),
        "modules_files": modules_files,
        "roar_inject_dir": _ROAR_INJECT_DIR,
        "shared_libs": _get_loaded_shared_libs(),
        "sys_prefix": sys.prefix,
        "sys_base_prefix": sys.base_prefix,
        "virtual_env": getattr(os.environ, _ORIGINAL_ENVIRON_GET_ATTR)("VIRTUAL_ENV", ""),
        "argv": sys.argv,
        "installed_packages": installed_packages,
        "used_packages": used_packages,
    }
    with _real_open(LOG_FILE, "w") as f:
        json.dump(data, f)


if os.environ.get("ROAR_WRAP") == "1":
    _runtime_import_controller.initialize_selected_backend()


atexit.register(_write_log)
