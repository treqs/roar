from __future__ import annotations

import atexit
import contextlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import uuid
from collections.abc import Mapping
from types import ModuleType
from typing import Any, cast

try:
    import tomllib
except ImportError:  # pragma: no cover
    import tomli as tomllib

from roar.backends.ray.env_contract import merge_worker_bootstrap_env
from roar.execution.framework.contract import ROAR_EXECUTION_BACKEND_ENV
from roar.services.execution.inject.support import SuppressTracking, warn_runtime
from roar.services.execution.worker_bootstrap import (
    WORKER_PY_EXECUTABLE,
    WORKER_SETUP_HOOK,
)
from roar.services.execution.worker_bootstrap import (
    prepare_worker_runtime_env as prepare_framework_worker_runtime_env,
)

_DEFAULT_RAY_NODE_POLL_INTERVAL_SECONDS = 5.0
_NODE_AGENT_RESOURCE_FRACTION = 0.0001
_WORKER_SETUP_HOOK_ENV_VAR = "__RAY_WORKER_PROCESS_SETUP_HOOK_ENV_VAR"

_ray_node_poller_lock = threading.Lock()
_ray_node_poller_stop = threading.Event()
_ray_node_poller_thread: threading.Thread | None = None
_ray_node_agents_lock = threading.Lock()
_ray_node_agents: dict[str, dict[str, Any]] = {}
_ray_collect_pre_shutdown_registered = False
_real_subprocess_popen = subprocess.Popen
_driver_phase_subprocess_patched = False
_driver_phase_s3_clients_patched = False
_driver_phase_counter = 0
_driver_phase_counter_lock = threading.Lock()
_runtime_process_initialized = False


def initialize_runtime_process() -> None:
    global _runtime_process_initialized

    if _runtime_process_initialized:
        return

    if _phase_capture_enabled():
        patch_driver_phase_subprocess_capture()

    if os.environ.get("ROAR_DRIVER_PHASE_PROXY_URL"):
        with contextlib.suppress(Exception):
            patch_driver_phase_s3_clients()

    atexit.register(_stop_ray_node_poller)
    _runtime_process_initialized = True


def observe_runtime_import(module_name: str, module: Any) -> None:
    del module

    global _driver_phase_s3_clients_patched
    if _driver_phase_s3_clients_patched or not os.environ.get("ROAR_DRIVER_PHASE_PROXY_URL"):
        return

    if module_name in {"boto3", "botocore"} or module_name.startswith(("boto3.", "botocore.")):
        patch_driver_phase_s3_clients()
        _driver_phase_s3_clients_patched = True


def patch_imported_ray_module(module_name: str, module: Any) -> None:
    del module_name

    ray_module = sys.modules.get("ray")
    if ray_module is None and hasattr(module, "init"):
        ray_module = cast(ModuleType, module)
    if ray_module is None or getattr(ray_module, "_roar_runtime_patched", False):
        return
    if not hasattr(ray_module, "init"):
        return

    patch_ray_init(ray_module)
    patch_ray_shutdown(ray_module)
    cast(Any, ray_module)._roar_runtime_patched = True


def patch_ray_init(ray_module: ModuleType) -> None:
    real_ray_init = ray_module.init

    def _roar_ray_init(*args, **kwargs):
        ray_config = load_ray_config()
        if not ray_config["enabled"]:
            return real_ray_init(*args, **kwargs)

        if os.environ.get("ROAR_JOB_INSTRUMENTED") == "1":
            runtime_env = dict(kwargs.pop("runtime_env", None) or {})
            submitted_job_id = (
                os.environ.get("ROAR_JOB_ID")
                or dict(runtime_env.get("env_vars", {}) or {}).get("ROAR_JOB_ID")
                or uuid.uuid4().hex[:8]
            )
            kwargs["runtime_env"] = sanitize_worker_runtime_env_for_ray(
                ray_module,
                _prepare_instrumented_job_worker_runtime_env(runtime_env, str(submitted_job_id)),
            )
            result = real_ray_init(*args, **kwargs)
            register_pre_shutdown_ray_collection()

            if node_agents_enabled():
                try:
                    print(f"[roar] spawning node agents for job {submitted_job_id}")
                    spawn_node_agents(ray_module, str(submitted_job_id))
                    print(f"[roar] node agents spawned (count={len(_ray_node_agents)})")
                except Exception as exc:
                    print(f"[roar] WARNING: _spawn_node_agents failed: {exc}")
                start_ray_node_poller(ray_module)
            return result

        runtime_env = dict(kwargs.pop("runtime_env", None) or {})
        env_vars = dict(runtime_env.get("env_vars", {}) or {})
        if ray_config["pip_install"]:
            runtime_env["pip"] = merge_roar_runtime_env_pip(runtime_env.get("pip"))
        else:
            runtime_env.pop("pip", None)

        job_id = (
            os.environ.get("ROAR_JOB_ID")
            or env_vars.get("ROAR_JOB_ID")
            or os.environ.get("RAY_JOB_ID")
        )
        if not job_id:
            job_id = uuid.uuid4().hex[:8]
        job_id = str(job_id)
        driver_job_uid = str(os.environ.get("ROAR_JOB_ID", ""))
        os.environ.setdefault("ROAR_JOB_ID", job_id)
        runtime_env["env_vars"] = prepare_worker_env_vars(
            runtime_env.get("env_vars", {}),
            job_id=job_id,
            driver_job_uid=driver_job_uid,
        )
        runtime_env = prepare_worker_runtime_env(runtime_env, job_id)
        runtime_env = sanitize_worker_runtime_env_for_ray(ray_module, runtime_env)
        kwargs["runtime_env"] = runtime_env
        result = real_ray_init(*args, **kwargs)
        register_pre_shutdown_ray_collection()
        if node_agents_enabled():
            threading.Thread(
                target=spawn_node_agents,
                args=(ray_module, job_id),
                name="roar-ray-node-agent-bootstrap",
                daemon=True,
            ).start()
            start_ray_node_poller(ray_module)
        return result

    cast(Any, ray_module).init = _roar_ray_init


def patch_ray_shutdown(ray_module: ModuleType) -> None:
    real_ray_shutdown = getattr(ray_module, "shutdown", None)
    if not callable(real_ray_shutdown):
        return
    if getattr(real_ray_shutdown, "_roar_patched", False):
        return

    def _roar_ray_shutdown(*args, **kwargs):
        proxy_logs = collect_node_agent_logs(ray_module) if node_agents_enabled() else {}
        collect_ray_io(proxy_logs=proxy_logs)
        return real_ray_shutdown(*args, **kwargs)

    _roar_ray_shutdown._roar_patched = True  # type: ignore[attr-defined]
    cast(Any, ray_module).shutdown = _roar_ray_shutdown


def prepare_worker_env_vars(
    existing_env_vars: Mapping[str, str] | None,
    *,
    job_id: str,
    driver_job_uid: str,
) -> dict[str, str]:
    return merge_worker_bootstrap_env(
        existing_env_vars,
        os.environ,
        job_id=job_id,
        driver_job_uid=str(driver_job_uid),
    )


def prepare_worker_runtime_env(runtime_env: Mapping[str, Any] | None, job_id: str) -> dict[str, Any]:
    backend_name = str(os.environ.get(ROAR_EXECUTION_BACKEND_ENV) or "").strip()
    if not backend_name:
        raise RuntimeError(f"{ROAR_EXECUTION_BACKEND_ENV} is required for worker bootstrap")
    runtime_env_out = prepare_framework_worker_runtime_env(
        backend_name,
        dict(runtime_env or {}),
        job_id,
        source_environ=os.environ,
    )
    env_vars = dict(runtime_env_out.get("env_vars", {}) or {})
    env_vars[_WORKER_SETUP_HOOK_ENV_VAR] = WORKER_SETUP_HOOK
    runtime_env_out["py_executable"] = WORKER_PY_EXECUTABLE
    runtime_env_out["worker_process_setup_hook"] = WORKER_SETUP_HOOK
    runtime_env_out["env_vars"] = env_vars
    return runtime_env_out


def sanitize_worker_runtime_env_for_ray(ray_module: ModuleType, runtime_env: Mapping[str, Any]) -> dict[str, Any]:
    runtime_env_out = dict(runtime_env or {})
    if not runtime_env_out.get("worker_process_setup_hook"):
        return runtime_env_out

    env_vars = dict(runtime_env_out.get("env_vars", {}) or {})
    if _WORKER_SETUP_HOOK_ENV_VAR not in env_vars:
        return runtime_env_out

    if _ray_rejects_manual_worker_setup_hook_env(ray_module):
        env_vars.pop(_WORKER_SETUP_HOOK_ENV_VAR, None)
        runtime_env_out["env_vars"] = env_vars

    return runtime_env_out


def node_agents_enabled() -> bool:
    raw = os.environ.get("ROAR_RAY_NODE_AGENTS", "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def spawn_node_agents(ray_module: ModuleType, job_id: str) -> None:
    try:
        from roar.backends.ray.node_agent import RoarNodeAgent, build_node_agent_name
    except Exception as exc:
        print(f"[roar] cannot import RoarNodeAgent: {exc}")
        return

    try:
        nodes = ray_module.nodes()
    except Exception as exc:
        print(f"[roar] ray.nodes() failed: {exc}")
        return

    alive_nodes = [node for node in nodes if isinstance(node, dict) and node.get("Alive")]
    print(f"[roar] cluster has {len(alive_nodes)} alive nodes (of {len(nodes)} total)")

    for node in alive_nodes:
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
            print(f"[roar] found existing agent {actor_name}")
        except Exception:
            remote_options: dict[str, Any] = {
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
                agent = cast(Any, RoarNodeAgent).options(**remote_options).remote(job_id=job_id)
                print(f"[roar] spawned agent {actor_name} on node {node_id[:8]}")
            except Exception as exc:
                print(f"[roar] FAILED to spawn agent {actor_name}: {exc}")
                agent = None

        if agent is not None:
            with _ray_node_agents_lock:
                _ray_node_agents[node_id] = {"name": actor_name, "actor": agent}

    with _ray_node_agents_lock:
        agents_to_wait = list(_ray_node_agents.values())
    for info in agents_to_wait:
        agent = info.get("actor")
        if agent is not None:
            with contextlib.suppress(Exception):
                ray_module.get(agent.get_proxy_port.remote(), timeout=15)


def collect_node_agent_logs(ray_module: ModuleType) -> dict[str, dict]:
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


def start_ray_node_poller(ray_module: ModuleType) -> None:
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


def load_ray_config() -> dict[str, object]:
    config_enabled = True
    config_pip_install = False

    try:
        from roar.config import load_config

        start_dir = os.environ.get("ROAR_PROJECT_DIR") or os.getcwd()
        config = load_config(start_dir=start_dir)
        ray_section = config.get("ray", {})
        if isinstance(ray_section, dict):
            config_enabled = bool(ray_section.get("enabled", True))

        explicit_pip_install = _load_explicit_ray_pip_install(start_dir)
        if explicit_pip_install is not None:
            config_pip_install = explicit_pip_install
    except Exception:
        pass

    return {"enabled": config_enabled, "pip_install": config_pip_install}


def merge_roar_runtime_env_pip(existing_pip: object) -> list[str]:
    pip_dependencies = _coerce_runtime_env_pip(existing_pip)
    pip_dependencies = [
        dep for dep in pip_dependencies if _requirement_name(dep) not in {"roar-cli", "roar"}
    ]
    pip_dependencies.append(_resolve_roar_requirement())
    return pip_dependencies


def register_pre_shutdown_ray_collection() -> None:
    global _ray_collect_pre_shutdown_registered

    if _ray_collect_pre_shutdown_registered:
        return

    atexit.register(shutdown_ray_at_exit)
    _ray_collect_pre_shutdown_registered = True


def shutdown_ray_at_exit() -> None:
    ray_module = sys.modules.get("ray")
    if ray_module is None:
        collect_ray_io()
        return

    shutdown = getattr(ray_module, "shutdown", None)
    is_initialized = getattr(ray_module, "is_initialized", None)
    if callable(is_initialized):
        with contextlib.suppress(Exception):
            if not is_initialized():
                return

    if callable(shutdown):
        with contextlib.suppress(Exception):
            shutdown()
            return

    collect_ray_io()


def collect_ray_io(proxy_logs: dict[str, dict] | None = None) -> None:
    if os.environ.get("ROAR_WRAP") != "1":
        return

    if proxy_logs is None and node_agents_enabled():
        ray_module = sys.modules.get("ray")
        if ray_module is not None:
            with contextlib.suppress(Exception):
                proxy_logs = collect_node_agent_logs(cast(ModuleType, ray_module))

    if not proxy_logs:
        return

    try:
        import time as runtime_time

        from roar.backends.ray.fragment import TaskFragment, derive_task_uid
        from roar.backends.ray.roar_worker import _parse_proxy_log_lines
    except Exception:
        return

    now = runtime_time.time()
    roar_job_id = str(os.environ.get("ROAR_JOB_ID", "default"))
    driver_job_uid = str(os.environ.get("ROAR_JOB_ID", ""))

    fragments: list[dict[str, object]] = []
    parsed_refs: list = []
    for node_id, payload in proxy_logs.items():
        if not isinstance(payload, dict):
            continue
        lines = payload.get("proxy_log_lines") or payload.get("entries") or []
        if not isinstance(lines, list):
            continue
        parsed = _parse_proxy_log_lines([str(line) for line in lines if line])
        if not parsed:
            continue

        parsed_refs.extend(parsed)
        runtime_node_id = str(payload.get("node_id") or node_id or "")
        proxy_task_id = f"proxy:{runtime_node_id or 'unknown'}"
        fragment = TaskFragment(
            job_uid=derive_task_uid(roar_job_id, proxy_task_id),
            parent_job_uid=driver_job_uid,
            ray_task_id=proxy_task_id,
            ray_worker_id="",
            ray_node_id=runtime_node_id,
            ray_actor_id=None,
            function_name="s3_proxy",
            started_at=now,
            ended_at=now,
            exit_code=0,
        )
        for kind, ref in parsed:
            if kind == "write":
                fragment.writes.append(ref)
            else:
                fragment.reads.append(ref)
        if fragment.reads or fragment.writes:
            fragments.append(fragment.to_dict())

    if not fragments:
        return

    from roar.services.execution.fragment_transport import emit_fragment_dicts

    emit_fragment_dicts(
        fragments,
        env=os.environ,
        local_fallback=lambda: _write_proxy_artifacts_to_db(parsed_refs),
    )


def patch_driver_phase_subprocess_capture() -> None:
    global _driver_phase_subprocess_patched

    if _driver_phase_subprocess_patched:
        return

    class _TrackedDriverPhasePopen(_real_subprocess_popen):  # type: ignore[misc, valid-type]
        def __init__(self, args, *popen_args, **popen_kwargs):
            capture = _build_driver_phase_capture(args, popen_kwargs)
            self._roar_phase_capture = capture
            self._roar_phase_started_at = None

            if capture is not None:
                child_env = dict(popen_kwargs.get("env") or os.environ)
                capture["env"] = child_env
                try:
                    from roar.services.execution.proxy import ProxyService

                    service = ProxyService()
                    handle = service.start_for_run(
                        session_id=str(os.environ.get("ROAR_SESSION_ID", "")).strip() or None,
                        job_id=str(os.environ.get("ROAR_JOB_ID", "")).strip() or None,
                        upstream_url=str(os.environ.get("ROAR_UPSTREAM_S3_ENDPOINT", "")).strip()
                        or None,
                    )
                    capture["service"] = service
                    capture["handle"] = handle
                    child_env["ROAR_DRIVER_PHASE_PROXY_URL"] = f"http://127.0.0.1:{handle.port}"
                except Exception as exc:
                    warn_runtime(
                        "Failed to start driver phase proxy for %s: %s",
                        capture["phase_label"],
                        exc,
                    )
                popen_kwargs["env"] = child_env

            super().__init__(args, *popen_args, **popen_kwargs)
            if capture is not None:
                self._roar_phase_started_at = __import__("time").time()

        def _roar_finalize(self) -> None:
            capture = getattr(self, "_roar_phase_capture", None)
            if not isinstance(capture, dict) or capture.get("finalized"):
                return
            returncode = _real_subprocess_popen.poll(self)
            if returncode is None:
                return
            capture["finalized"] = True
            started_at = float(self._roar_phase_started_at or __import__("time").time())
            ended_at = __import__("time").time()
            try:
                _emit_driver_phase_fragment(
                    capture,
                    exit_code=int(returncode),
                    started_at=started_at,
                    ended_at=ended_at,
                )
            except Exception as exc:
                warn_runtime(
                    "Failed to emit driver phase lineage for %s: %s",
                    capture.get("phase_label"),
                    exc,
                )

        def wait(self, *args, **kwargs):
            result = super().wait(*args, **kwargs)
            self._roar_finalize()
            return result

        def communicate(self, *args, **kwargs):
            result = super().communicate(*args, **kwargs)
            self._roar_finalize()
            return result

        def poll(self):
            result = super().poll()
            if result is not None:
                self._roar_finalize()
            return result

    subprocess.Popen = _TrackedDriverPhasePopen  # type: ignore[misc]
    _driver_phase_subprocess_patched = True


def patch_driver_phase_s3_clients() -> None:
    import boto3

    phase_proxy_url = str(os.environ.get("ROAR_DRIVER_PHASE_PROXY_URL", "")).strip()
    if not phase_proxy_url:
        return

    current_endpoint = str(os.environ.get("AWS_ENDPOINT_URL", "")).strip()
    session_client = getattr(boto3.session.Session, "client", None)
    if callable(session_client) and not getattr(
        session_client, "_roar_driver_phase_patched", False
    ):
        real_session_client = session_client

        def _session_client(self, service_name, *args, **kwargs):
            if service_name == "s3":
                requested_endpoint = str(kwargs.get("endpoint_url") or "").strip()
                if not requested_endpoint or requested_endpoint == current_endpoint:
                    kwargs["endpoint_url"] = phase_proxy_url
            return real_session_client(self, service_name, *args, **kwargs)

        _session_client._roar_driver_phase_patched = True  # type: ignore[attr-defined]
        boto3.session.Session.client = _session_client

    root_client = getattr(boto3, "client", None)
    if callable(root_client) and not getattr(root_client, "_roar_driver_phase_patched", False):
        real_root_client = root_client

        def _client(service_name, *args, **kwargs):
            if service_name == "s3":
                requested_endpoint = str(kwargs.get("endpoint_url") or "").strip()
                if not requested_endpoint or requested_endpoint == current_endpoint:
                    kwargs["endpoint_url"] = phase_proxy_url
            return real_root_client(service_name, *args, **kwargs)

        _client._roar_driver_phase_patched = True  # type: ignore[attr-defined]
        boto3.client = _client


def _prepare_instrumented_job_worker_runtime_env(
    runtime_env: Mapping[str, Any] | None,
    job_id: str,
) -> dict[str, Any]:
    del job_id
    runtime_env_out = dict(runtime_env or {})
    runtime_env_out["py_executable"] = WORKER_PY_EXECUTABLE
    return runtime_env_out


def _phase_capture_enabled() -> bool:
    raw = os.environ.get("ROAR_DRIVER_PHASE_CAPTURE", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _next_driver_phase_counter() -> int:
    global _driver_phase_counter
    with _driver_phase_counter_lock:
        _driver_phase_counter += 1
        return _driver_phase_counter


def _coerce_subprocess_argv(args: Any) -> list[str] | None:
    if not isinstance(args, (list, tuple)):
        return None
    return [str(item) for item in args if item is not None]


def _extract_state_file_arg(argv: list[str]) -> str | None:
    for index, value in enumerate(argv):
        if value == "--state-file" and index + 1 < len(argv):
            return argv[index + 1]
        if value.startswith("--state-file="):
            return value.split("=", 1)[1]
    return None


def _extract_phase_label(argv: list[str]) -> str | None:
    candidate = ""
    if len(argv) >= 3 and os.path.basename(argv[0]).startswith("python") and argv[1] == "-m":
        candidate = argv[2].split(".")[-1]
    elif len(argv) >= 2 and os.path.basename(argv[0]).startswith("python"):
        candidate = os.path.basename(argv[1])
    elif argv:
        candidate = os.path.basename(argv[0])

    candidate = os.path.splitext(candidate)[0]
    if candidate.startswith("run_"):
        candidate = candidate[len("run_") :]
    candidate = candidate.strip().lower()
    return candidate or None


def _load_json_file(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _coerce_phase_timestamp(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _resolve_driver_phase_timestamp(
    phase_label: str,
    state: dict[str, Any],
    *,
    suffix: str,
) -> float | None:
    normalized_phase = str(phase_label or "").strip().lower()
    for key in (
        f"{normalized_phase}_{suffix}" if normalized_phase else "",
        f"phase_{suffix}",
    ):
        if not key:
            continue
        resolved = _coerce_phase_timestamp(state.get(key))
        if resolved is not None:
            return resolved
    return None


def _append_driver_phase_state_refs(
    fragment: Any,
    phase_label: str,
    pre_state: dict[str, Any],
    post_state: dict[str, Any],
    env: Mapping[str, str],
) -> None:
    from roar.backends.ray.fragment import ArtifactRef
    from roar.backends.ray.s3_key_paths import build_s3_path_or_placeholder

    seen_reads = {str(ref.path) for ref in fragment.reads}
    seen_writes = {str(ref.path) for ref in fragment.writes}

    def _bucket_path(bucket_key: str, key: str | None) -> str | None:
        bucket = str(env.get(bucket_key) or os.environ.get(bucket_key) or "").strip()
        return build_s3_path_or_placeholder(
            key,
            bucket_name=bucket,
            bucket_hint=bucket_key,
        )

    def _append_read(path: str | None) -> None:
        if not path or path in seen_reads:
            return
        fragment.reads.append(
            ArtifactRef(
                path=path,
                hash=None,
                hash_algorithm="",
                size=0,
                capture_method="python",
            )
        )
        seen_reads.add(path)

    def _append_write(path: str | None) -> None:
        if not path or path in seen_writes:
            return
        fragment.writes.append(
            ArtifactRef(
                path=path,
                hash=None,
                hash_algorithm="",
                size=0,
                capture_method="python",
            )
        )
        seen_writes.add(path)

    pre_shards = [str(item) for item in pre_state.get("shard_keys", []) if item]
    post_shards = [str(item) for item in post_state.get("shard_keys", []) if item]
    pre_processed_key = str(pre_state.get("processed_key") or "").strip()
    post_processed_key = str(post_state.get("processed_key") or "").strip()
    pre_model_key = str(pre_state.get("model_key") or "").strip()
    post_model_key = str(post_state.get("model_key") or "").strip()
    pre_metrics_key = str(pre_state.get("metrics_key") or "").strip()
    post_metrics_key = str(post_state.get("metrics_key") or "").strip()
    pre_report_key = str(pre_state.get("report_key") or "").strip()
    post_report_key = str(post_state.get("report_key") or "").strip()

    if phase_label in {"training", "evaluation"}:
        for shard_key in pre_shards:
            _append_read(_bucket_path("S3_DATA_BUCKET", shard_key))

    if phase_label == "training" and pre_processed_key:
        _append_read(_bucket_path("S3_DATA_BUCKET", pre_processed_key))

    if phase_label == "evaluation" and pre_model_key:
        _append_read(_bucket_path("S3_MODELS_BUCKET", pre_model_key))

    if post_shards and post_shards != pre_shards:
        for shard_key in post_shards:
            _append_write(_bucket_path("S3_DATA_BUCKET", shard_key))

    if post_processed_key and post_processed_key != pre_processed_key:
        _append_write(_bucket_path("S3_DATA_BUCKET", post_processed_key))

    if post_model_key and post_model_key != pre_model_key:
        _append_write(_bucket_path("S3_MODELS_BUCKET", post_model_key))

    if post_metrics_key and post_metrics_key != pre_metrics_key:
        _append_write(_bucket_path("S3_RESULTS_BUCKET", post_metrics_key))

    if post_report_key and post_report_key != pre_report_key:
        _append_write(_bucket_path("S3_RESULTS_BUCKET", post_report_key))


def _build_driver_phase_capture(args: Any, kwargs: dict[str, Any]) -> dict[str, Any] | None:
    if not _phase_capture_enabled() or kwargs.get("shell"):
        return None

    argv = _coerce_subprocess_argv(args)
    if not argv:
        return None

    state_file = _extract_state_file_arg(argv)
    if not state_file:
        return None

    phase_label = _extract_phase_label(argv)
    if not phase_label:
        return None

    return {
        "phase_label": phase_label,
        "state_file": state_file,
        "phase_index": _next_driver_phase_counter(),
        "pre_state": _load_json_file(state_file),
        "service": None,
        "handle": None,
        "env": {},
        "finalized": False,
    }


def _emit_driver_phase_fragment(
    capture: dict[str, Any],
    *,
    exit_code: int,
    started_at: float,
    ended_at: float,
) -> None:
    from roar.backends.ray.fragment import TaskFragment, derive_task_uid
    from roar.backends.ray.proxy_fragments import build_proxy_fragment, emit_fragment

    service = capture.get("service")
    handle = capture.get("handle")
    entries = []
    if service is not None and handle is not None:
        entries = service.stop_for_run(handle)

    phase_label = str(capture.get("phase_label") or "")
    phase_index = int(capture.get("phase_index") or 0)
    task_id = f"driver_phase:{phase_label}:{phase_index}"
    roar_job_id = str(os.environ.get("ROAR_JOB_ID", "default"))
    post_state = _load_json_file(str(capture.get("state_file") or ""))
    resolved_started_at = (
        _resolve_driver_phase_timestamp(phase_label, post_state, suffix="started_at") or started_at
    )
    resolved_ended_at = (
        _resolve_driver_phase_timestamp(phase_label, post_state, suffix="ended_at") or ended_at
    )
    if resolved_ended_at < resolved_started_at:
        resolved_ended_at = max(float(ended_at), resolved_started_at)

    fragment = build_proxy_fragment(
        entries,
        function_name=phase_label,
        task_id=task_id,
        parent_job_uid=roar_job_id,
        started_at=resolved_started_at,
        ended_at=resolved_ended_at,
        exit_code=exit_code,
        recorded_at=resolved_ended_at,
    )
    if fragment is None:
        fragment = TaskFragment(
            job_uid=derive_task_uid(roar_job_id, task_id),
            parent_job_uid=roar_job_id,
            ray_task_id=task_id,
            ray_worker_id="",
            ray_node_id="driver",
            ray_actor_id=None,
            function_name=phase_label,
            started_at=resolved_started_at,
            ended_at=resolved_ended_at,
            exit_code=exit_code,
            recorded_at=resolved_ended_at,
        )

    _append_driver_phase_state_refs(
        fragment,
        phase_label=phase_label,
        pre_state=capture.get("pre_state") or {},
        post_state=post_state,
        env=capture.get("env") or {},
    )
    if fragment.reads or fragment.writes:
        emit_fragment(fragment)


def _ray_node_poller_loop(ray_module: ModuleType, seen_node_ids: set[str], poll_interval: float) -> None:
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


def _active_ray_node_ids(ray_module: ModuleType) -> set[str]:
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


def _prime_new_ray_nodes(ray_module: ModuleType, seen_node_ids: set[str]) -> None:
    current_node_ids = _active_ray_node_ids(ray_module)
    if not current_node_ids:
        return

    new_node_ids = sorted(current_node_ids - seen_node_ids)
    for node_id in new_node_ids:
        _prime_ray_node(ray_module, node_id)

    if new_node_ids:
        spawn_node_agents(ray_module, job_id=str(os.environ.get("ROAR_JOB_ID", "default")))

    seen_node_ids.update(current_node_ids)


def _prime_ray_node(ray_module: ModuleType, node_id: str) -> None:
    try:
        node_resource = _node_resource_key(ray_module, node_id)
        remote_options: dict[str, Any] = {"num_cpus": 0}
        if node_resource:
            remote_options["resources"] = {node_resource: 0.001}

        @ray_module.remote(**remote_options)
        def _roar_prime_task():
            return 1

        ray_module.get(_roar_prime_task.remote(), timeout=10)
    except Exception:
        pass


def _node_resource_key(ray_module: ModuleType, node_id: str) -> str | None:
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


def _load_explicit_ray_pip_install(start_dir: str) -> bool | None:
    try:
        from roar.core.settings import find_config_file
    except Exception:
        return None

    config_path = find_config_file(start_dir=start_dir)
    if config_path is None:
        return None

    try:
        with SuppressTracking(), open(config_path, "rb") as handle:
            payload = tomllib.load(handle)
    except Exception:
        return None

    if config_path.name == "pyproject.toml":
        payload = payload.get("tool", {}).get("roar", {})

    ray_section = payload.get("ray")
    if not isinstance(ray_section, dict) or "pip_install" not in ray_section:
        return None

    return bool(ray_section.get("pip_install"))


def _coerce_runtime_env_pip(value: object) -> list[str]:
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


def _ray_rejects_manual_worker_setup_hook_env(ray_module: ModuleType) -> bool:
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


def _write_proxy_artifacts_to_db(parsed: list[tuple[str, Any]]) -> None:
    project_dir = os.environ.get("ROAR_PROJECT_DIR", "")
    if not project_dir:
        for candidate in [os.getcwd(), os.environ.get("HOME", "")]:
            if candidate and os.path.isfile(os.path.join(candidate, ".roar", "roar.db")):
                project_dir = candidate
                break
    if not project_dir:
        return

    db_path = os.path.join(project_dir, ".roar", "roar.db")
    if not os.path.isfile(db_path):
        return

    try:
        import time as runtime_time

        conn = sqlite3.connect(db_path, timeout=10)
        try:
            cursor = conn.execute("PRAGMA table_info(artifacts)")
            columns = {row[1] for row in cursor.fetchall()}
            now = runtime_time.time()

            for _kind, ref in parsed:
                artifact_id = uuid.uuid4().hex
                fields = ["id", "size", "first_seen_at", "first_seen_path", "kind", "metadata"]
                values: list[Any] = [artifact_id, ref.size or 0, now, ref.path, "primitive", "{}"]

                if "path" in columns:
                    fields.append("path")
                    values.append(ref.path)
                if "hash" in columns:
                    fields.append("hash")
                    values.append(ref.hash)
                if "source_type" in columns:
                    fields.append("source_type")
                    values.append("s3" if ref.path.startswith("s3://") else None)
                if "capture_method" in columns:
                    fields.append("capture_method")
                    values.append(ref.capture_method or "proxy")

                placeholders = ", ".join("?" for _ in fields)
                field_list = ", ".join(fields)
                conn.execute(
                    f"INSERT OR IGNORE INTO artifacts ({field_list}) VALUES ({placeholders})",
                    values,
                )

                artifact_hash_tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                if ref.hash and "artifact_hashes" in artifact_hash_tables:
                    conn.execute(
                        "INSERT OR IGNORE INTO artifact_hashes (artifact_id, algorithm, digest) "
                        "VALUES (?, ?, ?)",
                        (artifact_id, ref.hash_algorithm or "etag", ref.hash),
                    )

            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def _stop_ray_node_poller() -> None:
    _ray_node_poller_stop.set()
