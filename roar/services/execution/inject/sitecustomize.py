import atexit
import builtins
import json
import os
import sys

# ------------------------------------------------------------------------------
# Data structures the parent will ingest
# ------------------------------------------------------------------------------

opened_files = set()
imported_modules = set()
env_reads = {}  # Changed to dict to store values

# File where parent told us to write logs
LOG_FILE = os.environ.get("ROAR_LOG_FILE")

# Directory where this sitecustomize.py lives (to exclude from tracking)
_ROAR_INJECT_DIR = os.path.dirname(os.path.abspath(__file__))

# ------------------------------------------------------------------------------
# Track open() calls
# ------------------------------------------------------------------------------
_real_open = builtins.open


def tracking_open(*args, **kwargs):
    try:
        path = args[0]
        opened_files.add(os.path.abspath(path))
    except Exception:
        pass
    return _real_open(*args, **kwargs)


builtins.open = tracking_open

# ------------------------------------------------------------------------------
# Track imports (and patch Ray when ROAR_WRAP=1)
# ------------------------------------------------------------------------------
_real_import = builtins.__import__
_ray_patched = False


def tracking_import(name, globals=None, locals=None, fromlist=(), level=0):
    global _ray_patched
    imported_modules.add(name)
    module = _real_import(name, globals, locals, fromlist, level)
    if (
        not _ray_patched
        and os.environ.get("ROAR_WRAP") == "1"
        and (name == "ray" or name.startswith("ray."))
    ):
        try:
            import sys as _sys
            _ray_module = _sys.modules.get("ray")
            if _ray_module is not None and hasattr(_ray_module, "init"):
                _patch_ray_init(_ray_module)
                _ray_patched = True
        except Exception:
            pass
    return module


builtins.__import__ = tracking_import

# ------------------------------------------------------------------------------
# Track environment variable reads
# ------------------------------------------------------------------------------

# Monkeypatch os.environ.get
if not hasattr(os.environ, "_original_get"):
    os.environ._original_get = os.environ.get


def patched_environ_get(key, default=None):
    # Detect if key exists in environment and capture its value
    if key in os.environ:
        env_reads[key] = os.environ[key]
    return os.environ._original_get(key, default)


os.environ.get = patched_environ_get


# ------------------------------------------------------------------------------
# On exit, write the record to LOG_FILE
# ------------------------------------------------------------------------------


def _get_loaded_shared_libs():
    """Get list of loaded shared libraries from /proc/self/maps."""
    libs = set()
    try:
        with _real_open("/proc/self/maps", "r") as f:
            for line in f:
                # Format: address perms offset dev inode pathname
                parts = line.split()
                if len(parts) >= 6:
                    path = parts[5]
                    if path.endswith(".so") or ".so." in path:
                        libs.add(path)
    except Exception:
        pass
    return sorted(libs)


def _get_installed_packages():
    """Get installed packages with versions from the current environment."""
    packages = {}
    try:
        from importlib.metadata import distributions

        for dist in distributions():
            name = dist.metadata.get("Name")
            version = dist.metadata.get("Version")
            if name and version:
                packages[name] = version
    except Exception:
        pass
    return packages


def _get_used_packages(modules_files, installed_packages):
    """
    Determine which installed packages were actually used based on loaded module files.

    Returns dict of package_name -> version for packages that were imported.
    """
    used = {}
    unversioned = {}  # Packages without metadata (e.g., maturin develop installs)

    # Build a mapping of site-packages subdirectories to package names
    # e.g., "torch" -> "torch", "numpy" -> "numpy"
    try:
        from importlib.metadata import distributions

        pkg_dirs = {}  # top-level directory name -> package name
        for dist in distributions():
            name = dist.metadata.get("Name")
            if not name:
                continue
            # Get the top-level packages/modules this distribution provides
            if dist.files:
                for f in dist.files:
                    parts = str(f).split("/")
                    if parts:
                        top_dir = parts[0]
                        # Skip metadata directories
                        if not top_dir.endswith(".dist-info") and not top_dir.endswith(".egg-info"):
                            pkg_dirs[top_dir] = name

        # Now check each loaded module file
        for fpath in modules_files:
            if "site-packages" in fpath:
                # Extract the part after site-packages
                idx = fpath.find("site-packages/")
                if idx >= 0:
                    after_sp = fpath[idx + len("site-packages/") :]
                    top_dir = after_sp.split("/")[0]
                    # Handle .py files at top level
                    if top_dir.endswith(".py"):
                        top_dir = top_dir[:-3]
                    if top_dir in pkg_dirs:
                        pkg_name = pkg_dirs[top_dir]
                        if pkg_name in installed_packages:
                            used[pkg_name] = installed_packages[pkg_name]
                    else:
                        # Package loaded from site-packages but not in metadata
                        # (e.g., maturin develop, manual installs)
                        # Skip __pycache__ and other non-package dirs
                        if not top_dir.startswith("_") and not top_dir.endswith(".so"):
                            unversioned[top_dir] = None
    except Exception:
        pass

    # Merge unversioned packages (with None version to indicate no metadata)
    for pkg_name in unversioned:
        if pkg_name not in used:
            used[pkg_name] = None

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
        # Pass environment info to parent for package manager detection
        "sys_prefix": sys.prefix,
        "sys_base_prefix": sys.base_prefix,
        "virtual_env": os.environ._original_get("VIRTUAL_ENV", ""),
        "argv": sys.argv,
        # Package info from the traced environment
        "installed_packages": installed_packages,
        "used_packages": used_packages,
    }
    with _real_open(LOG_FILE, "w") as f:
        json.dump(data, f)


# ------------------------------------------------------------------------------
# Ray integration (active only when ROAR_WRAP=1)
# ------------------------------------------------------------------------------

def _patch_ray_init(ray_module) -> None:  # noqa: ANN001
    """
    Monkey-patch ray.init so that roar's worker setup hook is injected
    into every Ray cluster the driver connects to.
    """
    _real_ray_init = ray_module.init

    def _roar_ray_init(*args, **kwargs):
        runtime_env = dict(kwargs.pop("runtime_env", None) or {})
        env_vars = dict(runtime_env.get("env_vars", {}) or {})
        env_vars["ROAR_WORKER"] = "1"
        env_vars["ROAR_LOG_DIR"] = os.environ.get("ROAR_LOG_DIR", "/shared/.roar-logs")
        runtime_env["env_vars"] = env_vars
        runtime_env["worker_process_setup_hook"] = "roar.ray.worker.setup"
        kwargs["runtime_env"] = runtime_env
        return _real_ray_init(*args, **kwargs)

    ray_module.init = _roar_ray_init


def _collect_ray_io() -> None:
    """Atexit hook: collect worker I/O logs and write to the roar DB."""
    if os.environ.get("ROAR_WRAP") != "1":
        return
    try:
        from roar.ray.collector import collect  # noqa: PLC0415
        collect(
            project_dir=os.environ.get("ROAR_PROJECT_DIR"),
            log_dir=os.environ.get("ROAR_LOG_DIR"),
        )
    except Exception:  # noqa: BLE001
        pass


atexit.register(_write_log)
atexit.register(_collect_ray_io)
