"""Helpers for capturing distributed lineage from Ray tasks and actors."""

from __future__ import annotations

import contextlib
import functools
import hashlib
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

ROAR_RAY_LINEAGE_ACTOR_ENV = "ROAR_RAY_LINEAGE_ACTOR"
ROAR_RAY_NAMESPACE_ENV = "ROAR_RAY_NAMESPACE"
ROAR_RAY_ADDRESS_ENV = "ROAR_RAY_ADDRESS"
ROAR_RAY_RUN_ID_ENV = "ROAR_RAY_RUN_ID"
ROAR_DISTRIBUTED_BACKEND_ENV = "ROAR_DISTRIBUTED_BACKEND"

_PROPAGATED_ENV_KEYS = (
    ROAR_DISTRIBUTED_BACKEND_ENV,
    ROAR_RAY_LINEAGE_ACTOR_ENV,
    ROAR_RAY_NAMESPACE_ENV,
    ROAR_RAY_ADDRESS_ENV,
    ROAR_RAY_RUN_ID_ENV,
)

_RAY_MODULE: Any | None = None
_LINEAGE_ACTOR_CLASS: Any | None = None
_ACTOR_CACHE: tuple[str, str, Any] | None = None

@dataclass
class _TaskLineage:
    task_name: str
    task_id: str | None = None
    attempt: int | None = None
    actor_id: str | None = None
    node_id: str | None = None
    job_id: str | None = None
    inputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    outputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    input_refs: set[str] = field(default_factory=set)
    output_refs: set[str] = field(default_factory=set)
    metadata: dict[str, Any] = field(default_factory=dict)

    def add_file(self, kind: str, descriptor: dict[str, Any]) -> None:
        path = str(descriptor.get("path") or "")
        if not path:
            return
        if kind == "input":
            self.inputs[path] = descriptor
            return
        self.outputs[path] = descriptor

    def add_ref(self, kind: str, ref: str) -> None:
        if not ref:
            return
        if kind == "input":
            self.input_refs.add(ref)
            return
        self.output_refs.add(ref)


_TASK_LINEAGE_BY_THREAD: dict[int, _TaskLineage] = {}


def _set_active_task(task: _TaskLineage) -> None:
    _TASK_LINEAGE_BY_THREAD[threading.get_ident()] = task


def _get_active_task() -> _TaskLineage | None:
    return _TASK_LINEAGE_BY_THREAD.get(threading.get_ident())


def _clear_active_task() -> None:
    _TASK_LINEAGE_BY_THREAD.pop(threading.get_ident(), None)


def _import_ray() -> Any:
    global _RAY_MODULE
    if _RAY_MODULE is None:
        import ray  # type: ignore[import-not-found]

        _RAY_MODULE = ray
    return _RAY_MODULE


def is_ray_available() -> bool:
    """Return True when Ray can be imported."""
    try:
        _import_ray()
    except Exception:
        return False
    return True


def _ensure_ray_initialized(address: str | None, namespace: str) -> Any:
    ray = _import_ray()
    if ray.is_initialized():
        return ray

    init_kwargs: dict[str, Any] = {
        "ignore_reinit_error": True,
        "namespace": namespace,
        "log_to_driver": False,
    }
    if address:
        init_kwargs["address"] = address
    else:
        init_kwargs["address"] = "auto"
    ray.init(**init_kwargs)
    return ray


def _get_runtime_hex(runtime: Any, getter: str) -> str | None:
    fn = getattr(runtime, getter, None)
    if not callable(fn):
        return None
    try:
        value = fn()
    except Exception:
        return None
    if value is None:
        return None
    hex_attr = getattr(value, "hex", None)
    if callable(hex_attr):
        with contextlib.suppress(Exception):
            return str(hex_attr())
    return str(value)


def _lineage_actor_class(ray: Any) -> Any:
    global _LINEAGE_ACTOR_CLASS
    if _LINEAGE_ACTOR_CLASS is not None:
        return _LINEAGE_ACTOR_CLASS

    @ray.remote(num_cpus=0)
    class _LineageEventActor:
        def __init__(self) -> None:
            self._events: list[dict[str, Any]] = []

        def emit(self, event: dict[str, Any]) -> int:
            self._events.append(event)
            return len(self._events)

        def get_events(self) -> list[dict[str, Any]]:
            return list(self._events)

        def clear(self) -> None:
            self._events.clear()

    _LINEAGE_ACTOR_CLASS = _LineageEventActor
    return _LINEAGE_ACTOR_CLASS


def _get_actor_handle(ray: Any, actor_name: str, namespace: str) -> Any:
    global _ACTOR_CACHE
    if _ACTOR_CACHE and _ACTOR_CACHE[0] == actor_name and _ACTOR_CACHE[1] == namespace:
        return _ACTOR_CACHE[2]
    actor = ray.get_actor(actor_name, namespace=namespace)
    _ACTOR_CACHE = (actor_name, namespace, actor)
    return actor


def create_lineage_actor(
    actor_name: str,
    namespace: str,
    address: str | None = None,
) -> tuple[bool, str | None]:
    """Create or reset a detached actor used to aggregate lineage events."""
    try:
        ray = _ensure_ray_initialized(address, namespace)
        with contextlib.suppress(Exception):
            existing = ray.get_actor(actor_name, namespace=namespace)
            ray.kill(existing, no_restart=True)

        actor_cls = _lineage_actor_class(ray)
        actor_cls.options(
            name=actor_name,
            namespace=namespace,
            lifetime="detached",
        ).remote()
        return True, None
    except Exception as exc:
        return False, str(exc)


def fetch_lineage_events(
    actor_name: str,
    namespace: str,
    address: str | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Fetch all collected lineage events from the detached actor."""
    try:
        ray = _ensure_ray_initialized(address, namespace)
        actor = _get_actor_handle(ray, actor_name, namespace)
        events = ray.get(actor.get_events.remote())
        if isinstance(events, list):
            return [event for event in events if isinstance(event, dict)], None
        return [], None
    except Exception as exc:
        return [], str(exc)


def destroy_lineage_actor(
    actor_name: str,
    namespace: str,
    address: str | None = None,
) -> tuple[bool, str | None]:
    """Destroy a detached lineage actor."""
    try:
        ray = _ensure_ray_initialized(address, namespace)
        actor = ray.get_actor(actor_name, namespace=namespace)
        ray.kill(actor, no_restart=True)
        global _ACTOR_CACHE
        _ACTOR_CACHE = None
        return True, None
    except Exception as exc:
        return False, str(exc)


def _lineage_env_vars() -> dict[str, str]:
    env_vars: dict[str, str] = {}
    for key in _PROPAGATED_ENV_KEYS:
        value = os.environ.get(key)
        if value:
            env_vars[key] = value
    return env_vars


def _hash_file_descriptor(path: str) -> dict[str, Any]:
    descriptor: dict[str, Any] = {"kind": "file", "path": path}
    path_obj = Path(path)
    try:
        descriptor["size"] = path_obj.stat().st_size
    except OSError:
        return descriptor

    try:
        from .db.hashing.backend import compute_hashes_batch

        hashes = compute_hashes_batch([str(path_obj)], ["blake3", "sha256"]).get(str(path_obj), {})
        if hashes:
            descriptor["hashes"] = hashes
    except Exception:
        pass
    return descriptor


def _emit_lineage_event(event: dict[str, Any]) -> bool:
    actor_name = os.environ.get(ROAR_RAY_LINEAGE_ACTOR_ENV)
    if not actor_name:
        return False

    namespace = os.environ.get(ROAR_RAY_NAMESPACE_ENV, "roar")
    address = os.environ.get(ROAR_RAY_ADDRESS_ENV)
    try:
        ray = _ensure_ray_initialized(address, namespace)
        actor = _get_actor_handle(ray, actor_name, namespace)
        ray.get(actor.emit.remote(event))
        return True
    except Exception:
        return False


def record_input(path: str) -> None:
    """Record a file input used by the active traced Ray task."""
    task = _get_active_task()
    if task is not None:
        task.add_file("input", _hash_file_descriptor(path))


def record_output(path: str) -> None:
    """Record a file output produced by the active traced Ray task."""
    task = _get_active_task()
    if task is not None:
        task.add_file("output", _hash_file_descriptor(path))


def record_input_ref(ref: str) -> None:
    """Record a logical Ray object/reference dependency input."""
    task = _get_active_task()
    if task is not None:
        task.add_ref("input", ref)


def record_output_ref(ref: str) -> None:
    """Record a logical Ray object/reference dependency output."""
    task = _get_active_task()
    if task is not None:
        task.add_ref("output", ref)


def _build_task_event(
    task: _TaskLineage,
    run_id: str,
    start_time: float,
    end_time: float,
    exit_code: int,
    error: str | None,
) -> dict[str, Any]:
    return {
        "version": 1,
        "backend": "ray",
        "run_id": run_id,
        "task_id": task.task_id,
        "task_name": task.task_name,
        "attempt": task.attempt,
        "actor_id": task.actor_id,
        "node_id": task.node_id,
        "job_id": task.job_id,
        "start_time": start_time,
        "end_time": end_time,
        "duration_seconds": max(0.0, end_time - start_time),
        "exit_code": exit_code,
        "error": error,
        "inputs": sorted(task.inputs.values(), key=lambda item: str(item.get("path", ""))),
        "outputs": sorted(task.outputs.values(), key=lambda item: str(item.get("path", ""))),
        "input_refs": sorted(task.input_refs),
        "output_refs": sorted(task.output_refs),
        "metadata": dict(task.metadata),
    }


def _wrap_traced_function(fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    def _wrapped(*args: Any, **kwargs: Any) -> Any:
        run_id = os.environ.get(ROAR_RAY_RUN_ID_ENV, "unknown")
        task_state = _TaskLineage(task_name=f"{fn.__module__}.{fn.__qualname__}")

        with contextlib.suppress(Exception):
            ray = _import_ray()
            runtime = ray.get_runtime_context()
            task_state.task_id = _get_runtime_hex(runtime, "get_task_id")
            task_state.actor_id = _get_runtime_hex(runtime, "get_actor_id")
            task_state.node_id = _get_runtime_hex(runtime, "get_node_id")
            task_state.job_id = _get_runtime_hex(runtime, "get_job_id")
            attempt = getattr(runtime, "task_attempt_number", None)
            if isinstance(attempt, int):
                task_state.attempt = attempt

        _set_active_task(task_state)
        start = time.time()
        exit_code = 0
        error: str | None = None
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            exit_code = 1
            error = str(exc)
            raise
        finally:
            end = time.time()
            event = _build_task_event(
                task=task_state,
                run_id=run_id,
                start_time=start,
                end_time=end,
                exit_code=exit_code,
                error=error,
            )
            _emit_lineage_event(event)
            _clear_active_task()

    return _wrapped


def traced_remote(*remote_args: Any, **remote_kwargs: Any) -> Any:
    """
    Ray ``remote`` wrapper that auto-captures task lineage events.

    Supports both ``@traced_remote`` and ``@traced_remote(...)`` forms.
    """
    ray = _import_ray()

    def _decorate(fn: Callable[..., Any]) -> Any:
        wrapped = _wrap_traced_function(fn)
        options = dict(remote_kwargs)

        runtime_env = dict(options.get("runtime_env") or {})
        env_vars = dict(runtime_env.get("env_vars") or {})
        for key, value in _lineage_env_vars().items():
            env_vars.setdefault(key, value)
        if env_vars:
            runtime_env["env_vars"] = env_vars
            options["runtime_env"] = runtime_env

        return ray.remote(*remote_args, **options)(wrapped)

    if len(remote_args) == 1 and callable(remote_args[0]) and not remote_kwargs:
        fn = cast(Callable[..., Any], remote_args[0])
        remote_args = ()
        return _decorate(fn)
    return _decorate


def ref_hashes(ref: str) -> dict[str, str]:
    """Build deterministic digest set for a logical Ray object reference."""
    materialized = f"ray_ref:{ref}".encode()
    try:
        import blake3  # type: ignore[import-not-found]

        blake3_digest = blake3.blake3(materialized).hexdigest()
    except Exception:
        # Fallback is only for environments missing blake3; key remains stable.
        blake3_digest = hashlib.blake2b(materialized, digest_size=32).hexdigest()

    return {
        "blake3": blake3_digest,
        "sha256": hashlib.sha256(materialized).hexdigest(),
    }
