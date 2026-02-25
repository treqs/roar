import atexit
import builtins
import importlib.metadata as importlib_metadata
import json
import os
import sys
import threading
import uuid

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
                _patch_ray_shutdown(_ray_module)
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

_DEFAULT_RAY_LOG_DIR = "/shared/.roar-logs"
_DEFAULT_RAY_NODE_POLL_INTERVAL_SECONDS = 5.0
_ray_node_poller_lock = threading.Lock()
_ray_node_poller_stop = threading.Event()
_ray_node_poller_thread = None


def _patch_ray_init(ray_module) -> None:  # noqa: ANN001
    """
    Monkey-patch ray.init so that roar's worker setup hook is injected
    into every Ray cluster the driver connects to.
    """
    _real_ray_init = ray_module.init

    def _roar_ray_init(*args, **kwargs):
        ray_config = _load_ray_config()
        if not ray_config["enabled"]:
            return _real_ray_init(*args, **kwargs)

        runtime_env = dict(kwargs.pop("runtime_env", None) or {})
        env_vars = dict(runtime_env.get("env_vars", {}) or {})
        if ray_config["pip_install"]:
            runtime_env["pip"] = _merge_roar_runtime_env_pip(runtime_env.get("pip"))
        else:
            runtime_env.pop("pip", None)

        job_id = os.environ.get("ROAR_JOB_ID") or env_vars.get("ROAR_JOB_ID")
        if not job_id:
            job_id = uuid.uuid4().hex[:8]
        job_id = str(job_id)

        env_vars["ROAR_WORKER"] = "1"
        env_vars["ROAR_LOG_DIR"] = ray_config["log_dir"]
        env_vars["ROAR_JOB_ID"] = job_id
        os.environ.setdefault("ROAR_LOG_DIR", ray_config["log_dir"])
        os.environ.setdefault("ROAR_JOB_ID", job_id)
        for key in (
            "AWS_ENDPOINT_URL",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "AWS_DEFAULT_REGION",
            "AWS_REGION",
        ):
            value = os.environ.get(key)
            if value:
                env_vars.setdefault(key, value)
        runtime_env["env_vars"] = env_vars
        runtime_env["worker_process_setup_hook"] = "roar.ray.worker.setup"
        kwargs["runtime_env"] = runtime_env
        result = _real_ray_init(*args, **kwargs)
        _start_ray_node_poller(ray_module)
        return result

    ray_module.init = _roar_ray_init


def _patch_ray_shutdown(ray_module) -> None:  # noqa: ANN001
    real_ray_shutdown = getattr(ray_module, "shutdown", None)
    if not callable(real_ray_shutdown):
        return

    def _roar_ray_shutdown(*args, **kwargs):
        _collect_ray_io()
        return real_ray_shutdown(*args, **kwargs)

    ray_module.shutdown = _roar_ray_shutdown


def _start_ray_node_poller(ray_module) -> None:  # noqa: ANN001
    global _ray_node_poller_thread

    if os.environ.get("ROAR_RAY_AUTOSCALING", "1").strip() in {"0", "false", "False"}:
        return

    with _ray_node_poller_lock:
        if _ray_node_poller_thread is not None and _ray_node_poller_thread.is_alive():
            return

        seen_node_ids = _active_ray_node_ids(ray_module)
        poll_interval = _ray_node_poll_interval_seconds()
        if poll_interval <= 0:
            return

        _ray_node_poller_stop.clear()
        _ray_node_poller_thread = threading.Thread(
            target=_ray_node_poller_loop,
            args=(ray_module, seen_node_ids, poll_interval),
            name="roar-ray-node-poller",
            daemon=True,
        )
        _ray_node_poller_thread.start()


def _ray_node_poller_loop(ray_module, seen_node_ids, poll_interval):  # noqa: ANN001
    while not _ray_node_poller_stop.wait(poll_interval):
        _prime_new_ray_nodes(ray_module, seen_node_ids)


def _ray_node_poll_interval_seconds() -> float:
    raw = os.environ.get("ROAR_RAY_NODE_POLL_INTERVAL")
    if not raw:
        return _DEFAULT_RAY_NODE_POLL_INTERVAL_SECONDS
    try:
        return float(raw)
    except ValueError:
        return _DEFAULT_RAY_NODE_POLL_INTERVAL_SECONDS


def _active_ray_node_ids(ray_module) -> set[str]:  # noqa: ANN001
    node_ids: set[str] = set()
    try:
        nodes = ray_module.nodes()
    except Exception:
        return node_ids

    for node in nodes:
        if not isinstance(node, dict) or not node.get("Alive"):
            continue
        node_id = node.get("NodeID")
        if node_id:
            node_ids.add(str(node_id))
    return node_ids


def _prime_new_ray_nodes(ray_module, seen_node_ids):  # noqa: ANN001
    current_node_ids = _active_ray_node_ids(ray_module)
    if not current_node_ids:
        return

    new_node_ids = sorted(current_node_ids - seen_node_ids)
    for node_id in new_node_ids:
        _prime_ray_node(ray_module, node_id)

    seen_node_ids.update(current_node_ids)


def _prime_ray_node(ray_module, node_id: str) -> None:  # noqa: ANN001
    try:
        node_resource = _node_resource_key(ray_module, node_id)
        remote_options = {"num_cpus": 0}
        if node_resource:
            remote_options["resources"] = {node_resource: 0.001}

        @ray_module.remote(**remote_options)
        def _roar_prime_task():  # noqa: ANN202
            return 1

        ray_module.get(_roar_prime_task.remote(), timeout=10)
    except Exception:
        pass


def _node_resource_key(ray_module, node_id: str) -> str | None:  # noqa: ANN001
    try:
        nodes = ray_module.nodes()
    except Exception:
        return None

    for node in nodes:
        if not isinstance(node, dict):
            continue
        if str(node.get("NodeID")) != node_id:
            continue
        resources = node.get("Resources")
        if not isinstance(resources, dict):
            return None
        for key in resources:
            key_text = str(key)
            if key_text.startswith("node:"):
                return key_text
        return None

    return None


def _load_ray_config() -> dict[str, object]:
    config_enabled = True
    config_pip_install = True
    config_log_dir = _DEFAULT_RAY_LOG_DIR

    try:
        from roar.config import load_config  # noqa: PLC0415

        start_dir = os.environ.get("ROAR_PROJECT_DIR") or os.getcwd()
        config = load_config(start_dir=start_dir)
        ray_section = config.get("ray", {})
        if isinstance(ray_section, dict):
            config_enabled = bool(ray_section.get("enabled", True))
            config_pip_install = bool(ray_section.get("pip_install", True))
            maybe_log_dir = ray_section.get("log_dir")
            if isinstance(maybe_log_dir, str) and maybe_log_dir.strip():
                config_log_dir = maybe_log_dir
    except Exception:
        pass

    env_log_dir = os.environ.get("ROAR_LOG_DIR")
    if env_log_dir:
        config_log_dir = env_log_dir

    return {
        "enabled": config_enabled,
        "pip_install": config_pip_install,
        "log_dir": config_log_dir,
    }


def _merge_roar_runtime_env_pip(existing_pip):  # noqa: ANN001
    pip_dependencies = _coerce_runtime_env_pip(existing_pip)
    pip_dependencies = [
        dep
        for dep in pip_dependencies
        if _requirement_name(dep) not in {"roar-cli", "roar"}
    ]
    pip_dependencies.append(_resolve_roar_requirement())
    return pip_dependencies


def _coerce_runtime_env_pip(value):  # noqa: ANN001
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if item]
    return []


def _requirement_name(requirement: str) -> str:
    text = requirement.strip()
    if not text:
        return ""

    for delimiter in ("@", "==", ">=", "<=", "~=", "!=", ">", "<", ";", "["):
        index = text.find(delimiter)
        if index > 0:
            text = text[:index]
            break

    return text.strip().lower()


def _resolve_roar_requirement() -> str:
    for package_name in ("roar-cli", "roar"):
        try:
            return f"{package_name}=={importlib_metadata.version(package_name)}"
        except importlib_metadata.PackageNotFoundError:
            continue
        except Exception:  # noqa: BLE001
            break

    return "roar-cli"


def _collect_ray_io() -> None:
    """Atexit hook: collect worker I/O logs and write to the roar DB."""
    if os.environ.get("ROAR_WRAP") != "1":
        return
    try:
        ray_config = _load_ray_config()
        log_dir = os.environ.get("ROAR_LOG_DIR", str(ray_config["log_dir"]))
        from roar.ray.collector import collect  # noqa: PLC0415
        collect(
            project_dir=os.environ.get("ROAR_PROJECT_DIR"),
            log_dir=log_dir,
        )
    except Exception:  # noqa: BLE001
        pass


def _stop_ray_node_poller() -> None:
    _ray_node_poller_stop.set()


atexit.register(_write_log)
atexit.register(_collect_ray_io)
atexit.register(_stop_ray_node_poller)
