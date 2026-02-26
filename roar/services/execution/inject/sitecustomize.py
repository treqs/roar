import atexit
import builtins
import importlib.metadata as importlib_metadata
import json
import os
import shutil
import sys
import tempfile
import textwrap
import threading
import uuid

try:
    import tomllib
except ImportError:  # pragma: no cover
    import tomli as tomllib

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
_NODE_AGENT_RESOURCE_FRACTION = 0.0001
_ray_node_poller_lock = threading.Lock()
_ray_node_poller_stop = threading.Event()
_ray_node_poller_thread = None
_ray_node_agents_lock = threading.Lock()
_ray_node_agents = {}


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
        env_vars["ROAR_LOG_BACKEND"] = "actor"
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
        runtime_env = _prepare_worker_runtime_env(runtime_env, job_id)
        kwargs["runtime_env"] = runtime_env
        result = _real_ray_init(*args, **kwargs)
        _ensure_collector_actor(ray_module, job_id)
        if _node_agents_enabled():
            threading.Thread(
                target=_spawn_node_agents,
                args=(ray_module, job_id, str(ray_config["log_dir"])),
                name="roar-ray-node-agent-bootstrap",
                daemon=True,
            ).start()
            _start_ray_node_poller(ray_module)
        return result

    ray_module.init = _roar_ray_init


def _patch_ray_shutdown(ray_module) -> None:  # noqa: ANN001
    real_ray_shutdown = getattr(ray_module, "shutdown", None)
    if not callable(real_ray_shutdown):
        return

    def _roar_ray_shutdown(*args, **kwargs):
        proxy_logs = _collect_node_agent_logs(ray_module) if _node_agents_enabled() else {}
        _collect_ray_io(proxy_logs=proxy_logs)
        return real_ray_shutdown(*args, **kwargs)

    ray_module.shutdown = _roar_ray_shutdown


def _ensure_collector_actor(ray_module, job_id: str) -> None:  # noqa: ANN001
    actor_name = f"roar-log-collector-{job_id}"

    try:
        ray_module.get_actor(actor_name, namespace="roar")
        return
    except Exception:  # noqa: BLE001
        pass

    try:
        from roar.ray.actor import RoarLogCollectorActor  # noqa: PLC0415

        actor = RoarLogCollectorActor.options(
            name=actor_name,
            namespace="roar",
            lifetime="detached",
            num_cpus=0,
        ).remote()
        get_fn = getattr(ray_module, "get", None)
        if callable(get_fn):
            get_all = getattr(actor, "get_all", None)
            remote = getattr(get_all, "remote", None) if get_all is not None else None
            if callable(remote):
                get_fn(remote(), timeout=10)
    except Exception:  # noqa: BLE001
        pass


def _node_agents_enabled() -> bool:
    raw = os.environ.get("ROAR_RAY_NODE_AGENTS", "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _prepare_worker_runtime_env(runtime_env, job_id: str):  # noqa: ANN001
    runtime_env = dict(runtime_env or {})
    tmp_dir = tempfile.mkdtemp(prefix=f"roar-worker-env-{job_id[:8]}-")

    existing_working_dir = runtime_env.get("working_dir")
    if isinstance(existing_working_dir, str) and existing_working_dir.strip():
        if os.path.isdir(existing_working_dir):
            _merge_working_dir(existing_working_dir, tmp_dir)
        else:
            _warn_roar(
                "Skipping working_dir merge for non-local path %s while preparing Ray worker wrapper",
                existing_working_dir,
            )

    try:
        from pathlib import Path  # noqa: PLC0415

        import roar  # noqa: PLC0415
        from roar.services.execution.tracer_backends import find_preload_library  # noqa: PLC0415

        roar_package_dir = Path(roar.__file__).resolve().parent
        shutil.copytree(roar_package_dir, os.path.join(tmp_dir, "roar"), dirs_exist_ok=True)

        preload_library = find_preload_library(roar_package_dir)
        if preload_library:
            shutil.copy2(preload_library, os.path.join(tmp_dir, "libroar_tracer_preload.so"))
    except Exception:
        pass

    wrapper_path = os.path.join(tmp_dir, "roar_worker_wrapper.sh")
    with _real_open(wrapper_path, "w", encoding="utf-8") as handle:
        handle.write(
            textwrap.dedent(
                """
                #!/bin/bash
                if [ -f "./libroar_tracer_preload.so" ]; then
                    export LD_PRELOAD="$(pwd)/libroar_tracer_preload.so"
                fi
                exec python3 "$@"
                """
            ).strip()
            + "\n"
        )
    os.chmod(wrapper_path, 0o755)

    worker_sitecustomize_path = os.path.join(tmp_dir, "sitecustomize.py")
    with _real_open(worker_sitecustomize_path, "w", encoding="utf-8") as handle:
        handle.write(
            textwrap.dedent(
                """
                import os
                import sys

                is_worker_process = any("default_worker.py" in arg for arg in sys.argv)
                if os.environ.get("ROAR_WORKER") == "1" and is_worker_process:
                    try:
                        from roar.ray.worker import setup as _roar_worker_setup

                        _roar_worker_setup()
                    except Exception:
                        pass
                """
            ).strip()
            + "\n"
        )

    runtime_env["working_dir"] = tmp_dir
    runtime_env["py_executable"] = "bash ./roar_worker_wrapper.sh"
    return runtime_env


def _merge_working_dir(source_dir: str, target_dir: str) -> None:
    for entry in os.listdir(source_dir):
        src = os.path.join(source_dir, entry)
        dst = os.path.join(target_dir, entry)
        try:
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)
        except Exception:
            continue


def _warn_roar(message: str, *args) -> None:
    text = message % args if args else message
    try:
        from roar.core.logging import get_logger  # noqa: PLC0415

        get_logger().warning(text)
        return
    except Exception:
        pass

    try:
        sys.stderr.write(text + "\n")
    except Exception:
        pass


def _spawn_node_agents(ray_module, job_id: str, log_dir: str) -> None:  # noqa: ANN001
    try:
        from roar.ray.node_agent import RoarNodeAgent, build_node_agent_name  # noqa: PLC0415
    except Exception:
        return

    try:
        nodes = ray_module.nodes()
    except Exception:
        return

    for node in nodes:
        if not isinstance(node, dict) or not node.get("Alive"):
            continue

        node_id = str(node.get("NodeID") or "")
        if not node_id:
            continue

        with _ray_node_agents_lock:
            if node_id in _ray_node_agents:
                continue

        actor_name = build_node_agent_name(job_id, node_id)
        agent = None
        try:
            agent = ray_module.get_actor(actor_name, namespace="roar")
        except Exception:
            remote_options = {
                "name": actor_name,
                "namespace": "roar",
                "lifetime": "detached",
                "num_cpus": 0,
            }

            node_resource = _node_resource_key(ray_module, node_id)
            if node_resource:
                remote_options["resources"] = {node_resource: _NODE_AGENT_RESOURCE_FRACTION}
            else:
                remote_options["resources"] = {f"node:{node_id}": _NODE_AGENT_RESOURCE_FRACTION}

            try:
                agent = RoarNodeAgent.options(**remote_options).remote(
                    job_id=job_id,
                    log_dir=log_dir,
                )
            except Exception:
                agent = None

        if agent is not None:
            with _ray_node_agents_lock:
                _ray_node_agents[node_id] = {"name": actor_name, "actor": agent}


def _collect_node_agent_logs(ray_module) -> dict[str, dict]:  # noqa: ANN001
    with _ray_node_agents_lock:
        node_agents = dict(_ray_node_agents)
        _ray_node_agents.clear()

    proxy_logs: dict[str, dict] = {}
    for node_id, info in node_agents.items():
        if not isinstance(info, dict):
            continue
        agent = info.get("actor")
        if agent is None:
            continue

        try:
            payload = ray_module.get(agent.collect_logs.remote(), timeout=15)
            if isinstance(payload, dict):
                payload.setdefault("node_id", node_id)
                proxy_logs[node_id] = payload
        except Exception:
            pass
        finally:
            try:
                ray_module.get(agent.shutdown.remote(), timeout=5)
            except Exception:
                pass
            try:
                ray_module.kill(agent)
            except Exception:
                pass

    return proxy_logs


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

    if new_node_ids:
        _spawn_node_agents(
            ray_module,
            job_id=str(os.environ.get("ROAR_JOB_ID", "default")),
            log_dir=str(os.environ.get("ROAR_LOG_DIR", _DEFAULT_RAY_LOG_DIR)),
        )

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
    config_pip_install = False
    config_log_dir = _DEFAULT_RAY_LOG_DIR

    try:
        from roar.config import load_config  # noqa: PLC0415

        start_dir = os.environ.get("ROAR_PROJECT_DIR") or os.getcwd()
        config = load_config(start_dir=start_dir)
        ray_section = config.get("ray", {})
        if isinstance(ray_section, dict):
            config_enabled = bool(ray_section.get("enabled", True))
            maybe_log_dir = ray_section.get("log_dir")
            if isinstance(maybe_log_dir, str) and maybe_log_dir.strip():
                config_log_dir = maybe_log_dir

        explicit_pip_install = _load_explicit_ray_pip_install(start_dir)
        if explicit_pip_install is not None:
            config_pip_install = explicit_pip_install
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


def _load_explicit_ray_pip_install(start_dir: str) -> bool | None:
    try:
        from roar.core.settings import find_config_file  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None

    config_path = find_config_file(start_dir=start_dir)
    if config_path is None:
        return None

    try:
        with _real_open(config_path, "rb") as handle:
            payload = tomllib.load(handle)
    except Exception:  # noqa: BLE001
        return None

    if config_path.name == "pyproject.toml":
        payload = payload.get("tool", {}).get("roar", {})

    ray_section = payload.get("ray")
    if not isinstance(ray_section, dict) or "pip_install" not in ray_section:
        return None

    return bool(ray_section.get("pip_install"))


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


def _collect_ray_io(proxy_logs: dict[str, dict] | None = None) -> None:
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
            proxy_logs=proxy_logs or {},
        )
    except Exception:  # noqa: BLE001
        pass


def _stop_ray_node_poller() -> None:
    _ray_node_poller_stop.set()


atexit.register(_write_log)
atexit.register(_collect_ray_io)
atexit.register(_stop_ray_node_poller)
