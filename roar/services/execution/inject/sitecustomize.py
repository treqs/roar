import atexit
import builtins
import contextlib
import json
import os
import sys
import tempfile
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


_roar_suppress = threading.local()


def _is_suppressed() -> bool:
    return bool(getattr(_roar_suppress, "active", False))


class _SuppressTracking:
    """Context manager: open() calls inside are not tracked."""

    def __enter__(self):
        _roar_suppress.active = True
        return self

    def __exit__(self, *_):
        _roar_suppress.active = False


def tracking_open(*args, **kwargs):
    if _is_suppressed():
        return _real_open(*args, **kwargs)
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
    """
    Determine which installed packages were actually used based on loaded module files.

    Returns dict of package_name -> version for packages that were imported.
    """
    used = {}

    try:
        from importlib import metadata as importlib_metadata

        # packages_distributions() returns {top_level_module: [NormalizedName, ...]}.
        # This is an index lookup, not a per-package RECORD file scan.
        pkg_dist_map = importlib_metadata.packages_distributions()
    except Exception:
        # Fall back to empty map; used packages won't be recorded but won't crash.
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
            # Skip metadata and cache dirs
            if top_dir.endswith(".dist-info") or top_dir.endswith(".egg-info"):
                continue
            if top_dir.startswith("_") or top_dir.endswith(".so"):
                continue

            pkg_names = pkg_dist_map.get(top_dir, [])
            for pkg_name in pkg_names:
                if pkg_name in installed_packages and pkg_name not in used:
                    used[pkg_name] = installed_packages[pkg_name]

            # If not in pkg_dist_map, record as unversioned (same as before).
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
_WORKER_SETUP_HOOK_ENV_VAR = "__RAY_WORKER_PROCESS_SETUP_HOOK_ENV_VAR"
_WORKER_SETUP_HOOK = "roar.ray.roar_worker._startup"
_ray_node_poller_lock = threading.Lock()
_ray_node_poller_stop = threading.Event()
_ray_node_poller_thread = None
_ray_node_agents_lock = threading.Lock()
_ray_node_agents = {}
_ray_collect_pre_shutdown_registered = False


def _patch_ray_init(ray_module) -> None:
    """
    Monkey-patch ray.init so that roar's worker setup hook is injected
    into every Ray cluster the driver connects to.
    """
    _real_ray_init = ray_module.init

    def _roar_ray_init(*args, **kwargs):
        ray_config = _load_ray_config()
        if not ray_config["enabled"]:
            return _real_ray_init(*args, **kwargs)

        if os.environ.get("ROAR_JOB_INSTRUMENTED") == "1":
            submitted_runtime_env = kwargs.get("runtime_env")
            submitted_env_vars = {}
            if isinstance(submitted_runtime_env, dict):
                submitted_env_vars = dict(submitted_runtime_env.get("env_vars", {}) or {})
            submitted_job_id = (
                os.environ.get("ROAR_JOB_ID")
                or submitted_env_vars.get("ROAR_JOB_ID")
                or os.environ.get("RAY_JOB_ID")
            )
            if not submitted_job_id:
                submitted_job_id = uuid.uuid4().hex[:8]

            result = _real_ray_init(*args, **kwargs)
            _ensure_collector_actor(ray_module, str(submitted_job_id))
            return result

        runtime_env = dict(kwargs.pop("runtime_env", None) or {})
        env_vars = dict(runtime_env.get("env_vars", {}) or {})
        if ray_config["pip_install"]:
            runtime_env["pip"] = _merge_roar_runtime_env_pip(runtime_env.get("pip"))
        else:
            runtime_env.pop("pip", None)

        job_id = os.environ.get("ROAR_JOB_ID") or env_vars.get("ROAR_JOB_ID") or os.environ.get(
            "RAY_JOB_ID"
        )
        if not job_id:
            job_id = uuid.uuid4().hex[:8]
        job_id = str(job_id)
        driver_job_uid = str(os.environ.get("ROAR_JOB_ID", ""))

        env_vars["ROAR_WORKER"] = "1"
        env_vars["ROAR_LOG_DIR"] = ray_config["log_dir"]
        env_vars["ROAR_LOG_BACKEND"] = "actor"
        env_vars["ROAR_JOB_ID"] = job_id
        env_vars["ROAR_DRIVER_JOB_UID"] = driver_job_uid
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
        runtime_env = _prepare_worker_runtime_env(runtime_env, job_id)
        runtime_env = _sanitize_worker_runtime_env_for_ray(ray_module, runtime_env)
        kwargs["runtime_env"] = runtime_env
        result = _real_ray_init(*args, **kwargs)
        _register_pre_shutdown_ray_collection()
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


def _patch_ray_shutdown(ray_module) -> None:
    real_ray_shutdown = getattr(ray_module, "shutdown", None)
    if not callable(real_ray_shutdown):
        return

    def _roar_ray_shutdown(*args, **kwargs):
        proxy_logs = _collect_node_agent_logs(ray_module) if _node_agents_enabled() else {}
        _collect_ray_io(proxy_logs=proxy_logs)
        return real_ray_shutdown(*args, **kwargs)

    ray_module.shutdown = _roar_ray_shutdown


def _ensure_collector_actor(ray_module, job_id: str) -> None:
    actor_name = f"roar-log-collector-{job_id}"

    try:
        ray_module.get_actor(actor_name, namespace="roar")
        return
    except Exception:
        pass

    try:
        from roar.ray.actor import RoarLogCollectorActor

        session_id = os.environ.get("ROAR_SESSION_ID")
        fragment_token = os.environ.get("ROAR_FRAGMENT_TOKEN")
        glaas_url = os.environ.get("GLAAS_URL") or os.environ.get("GLAAS_API_URL")

        actor_options = RoarLogCollectorActor.options(
            name=actor_name,
            namespace="roar",
            lifetime="detached",
            num_cpus=0,
        )

        if session_id and fragment_token and glaas_url:
            actor = actor_options.remote(
                session_id=session_id,
                token=fragment_token,
                glaas_url=glaas_url,
            )
        else:
            actor = actor_options.remote()

        get_fn = getattr(ray_module, "get", None)
        if callable(get_fn):
            get_all = getattr(actor, "get_all", None)
            remote = getattr(get_all, "remote", None) if get_all is not None else None
            if callable(remote):
                get_fn(remote(), timeout=10)
    except Exception:
        pass


def _node_agents_enabled() -> bool:
    raw = os.environ.get("ROAR_RAY_NODE_AGENTS", "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _prepare_worker_runtime_env(runtime_env, job_id: str):
    import shutil

    runtime_env = dict(runtime_env or {})
    prepared_working_dir: str | None = None

    existing_working_dir = runtime_env.get("working_dir")
    if isinstance(existing_working_dir, str) and existing_working_dir.strip():
        if os.path.isdir(existing_working_dir):
            prepared_working_dir = tempfile.mkdtemp(prefix=f"roar-worker-env-{job_id[:8]}-")
            with _SuppressTracking():
                _merge_working_dir(existing_working_dir, prepared_working_dir)
        else:
            _warn_roar(
                "Skipping working_dir merge for non-local path %s while preparing Ray worker wrapper",
                existing_working_dir,
            )
            prepared_working_dir = existing_working_dir

    if not prepared_working_dir:
        prepared_working_dir = tempfile.mkdtemp(prefix=f"roar-worker-env-{job_id[:8]}-")

    if os.path.isdir(prepared_working_dir):
        try:
            from pathlib import Path

            import roar
            from roar.services.execution.tracer_backends import find_preload_library

            roar_package_dir = Path(roar.__file__).resolve().parent
            with _SuppressTracking():
                shutil.copytree(
                    roar_package_dir,
                    os.path.join(prepared_working_dir, "roar"),
                    dirs_exist_ok=True,
                )

                preload_library = find_preload_library(roar_package_dir)
                if preload_library:
                    shutil.copy2(
                        preload_library,
                        os.path.join(prepared_working_dir, "libroar_tracer_preload.so"),
                    )
        except Exception:
            pass

    env_vars = dict(runtime_env.get("env_vars", {}) or {})
    env_vars[_WORKER_SETUP_HOOK_ENV_VAR] = _WORKER_SETUP_HOOK
    runtime_env["working_dir"] = prepared_working_dir
    runtime_env["py_executable"] = "roar-worker"
    runtime_env["worker_process_setup_hook"] = _WORKER_SETUP_HOOK
    runtime_env["env_vars"] = env_vars
    return runtime_env


def _ray_rejects_manual_worker_setup_hook_env(ray_module) -> bool:
    try:
        import inspect

        setup_hook_module = ray_module._private.runtime_env.setup_hook
        export_setup_func_module = getattr(setup_hook_module, "export_setup_func_module", None)
        if not callable(export_setup_func_module):
            return False
        source = inspect.getsource(export_setup_func_module)
    except Exception:
        return False

    return "is not permitted because it is reserved for the internal use" in source


def _sanitize_worker_runtime_env_for_ray(ray_module, runtime_env):
    runtime_env = dict(runtime_env or {})
    if not runtime_env.get("worker_process_setup_hook"):
        return runtime_env

    env_vars = dict(runtime_env.get("env_vars", {}) or {})
    if _WORKER_SETUP_HOOK_ENV_VAR not in env_vars:
        return runtime_env

    if _ray_rejects_manual_worker_setup_hook_env(ray_module):
        env_vars.pop(_WORKER_SETUP_HOOK_ENV_VAR, None)
        runtime_env["env_vars"] = env_vars

    return runtime_env


def _write_worker_wrapper(tmp_dir: str) -> None:
    wrapper_path = os.path.join(tmp_dir, "roar_worker_wrapper.sh")
    try:
        with _real_open(wrapper_path, "w", encoding="utf-8") as handle:
            handle.write(
                "#!/usr/bin/env bash\n"
                'if [ -f "./libroar_tracer_preload.so" ]; then\n'
                '    export LD_PRELOAD="$(pwd)/libroar_tracer_preload.so"\n'
                "fi\n"
                'exec python3 "$@"\n'
            )
        os.chmod(wrapper_path, 0o755)
    except Exception:
        pass


def _merge_working_dir(source_dir: str, target_dir: str) -> None:
    import shutil

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
        from roar.core.logging import get_logger

        get_logger().warning(text)
        return
    except Exception:
        pass

    with contextlib.suppress(Exception):
        sys.stderr.write(text + "\n")


def _spawn_node_agents(ray_module, job_id: str, log_dir: str) -> None:
    try:
        from roar.ray.node_agent import RoarNodeAgent, build_node_agent_name
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


def _collect_node_agent_logs(ray_module) -> dict[str, dict]:
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
            with contextlib.suppress(Exception):
                ray_module.get(agent.shutdown.remote(), timeout=5)
            with contextlib.suppress(Exception):
                ray_module.kill(agent)

    return proxy_logs


def _start_ray_node_poller(ray_module) -> None:
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


def _ray_node_poller_loop(ray_module, seen_node_ids, poll_interval):
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


def _active_ray_node_ids(ray_module) -> set[str]:
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


def _prime_new_ray_nodes(ray_module, seen_node_ids):
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


def _prime_ray_node(ray_module, node_id: str) -> None:
    try:
        node_resource = _node_resource_key(ray_module, node_id)
        remote_options = {"num_cpus": 0}
        if node_resource:
            remote_options["resources"] = {node_resource: 0.001}

        @ray_module.remote(**remote_options)
        def _roar_prime_task():
            return 1

        ray_module.get(_roar_prime_task.remote(), timeout=10)
    except Exception:
        pass


def _node_resource_key(ray_module, node_id: str) -> str | None:
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
        from roar.config import load_config

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
        from roar.core.settings import find_config_file
    except Exception:
        return None

    config_path = find_config_file(start_dir=start_dir)
    if config_path is None:
        return None

    try:
        with _real_open(config_path, "rb") as handle:
            payload = tomllib.load(handle)
    except Exception:
        return None

    if config_path.name == "pyproject.toml":
        payload = payload.get("tool", {}).get("roar", {})

    ray_section = payload.get("ray")
    if not isinstance(ray_section, dict) or "pip_install" not in ray_section:
        return None

    return bool(ray_section.get("pip_install"))


def _merge_roar_runtime_env_pip(existing_pip):
    pip_dependencies = _coerce_runtime_env_pip(existing_pip)
    pip_dependencies = [
        dep for dep in pip_dependencies if _requirement_name(dep) not in {"roar-cli", "roar"}
    ]
    pip_dependencies.append(_resolve_roar_requirement())
    return pip_dependencies


def _coerce_runtime_env_pip(value):
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
    import importlib.metadata as importlib_metadata

    for package_name in ("roar-cli", "roar"):
        try:
            return f"{package_name}=={importlib_metadata.version(package_name)}"
        except importlib_metadata.PackageNotFoundError:
            continue
        except Exception:
            break

    return "roar-cli"


def _register_pre_shutdown_ray_collection() -> None:
    """
    Register a collection hook after ray.init().

    Ray registers its own shutdown hook during init. Registering this hook
    afterwards ensures worker logs are collected before Ray tears down.
    """
    global _ray_collect_pre_shutdown_registered

    if _ray_collect_pre_shutdown_registered:
        return

    atexit.register(_collect_ray_io)
    _ray_collect_pre_shutdown_registered = True


def _collect_ray_io(proxy_logs: dict[str, dict] | None = None) -> None:
    """Atexit hook: collect worker I/O logs and write to the roar DB."""
    if os.environ.get("ROAR_WRAP") != "1":
        return
    try:
        log_dir = os.environ.get("ROAR_LOG_DIR")
        if not log_dir:
            ray_config = _load_ray_config()
            log_dir = str(ray_config["log_dir"])

        # Fast path: skip the heavy collector import if there's nothing to collect.
        # Worker fragment files are .json files written to log_dir by Ray workers.
        has_worker_logs = os.path.isdir(log_dir) and any(
            f.endswith(".json") for f in os.listdir(log_dir)
        )
        if not has_worker_logs and not proxy_logs:
            return

        from roar.ray.collector import collect

        collect(
            project_dir=os.environ.get("ROAR_PROJECT_DIR"),
            log_dir=log_dir,
            proxy_logs=proxy_logs or {},
        )
    except Exception:
        pass


def _stop_ray_node_poller() -> None:
    _ray_node_poller_stop.set()


atexit.register(_write_log)
atexit.register(_collect_ray_io)
atexit.register(_stop_ray_node_poller)
