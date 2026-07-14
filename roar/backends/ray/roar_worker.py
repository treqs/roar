from __future__ import annotations

import atexit
import builtins
import collections
import contextlib
import functools
import hashlib
import io
import os
import queue
import re
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from roar.backends.ray.collector import collect_fragments
from roar.backends.ray.config import load_ray_backend_config
from roar.backends.ray.fragment import ArtifactRef, TaskFragment, derive_task_uid
from roar.execution.cluster.proxy_config import local_proxy_endpoint, local_proxy_port_from_env
from roar.execution.fragments.transport import emit_fragment_dicts
from roar.integrations.glaas import GlaasFragmentStreamer

IOEvent = collections.namedtuple(
    "IOEvent",
    [
        "kind",
        "task_id",
        "function_name",
        "path",
        "hash_value",
        "hash_algorithm",
        "size",
        "capture_method",
    ],
)

_event_queue: queue.Queue[IOEvent | None] = queue.Queue()
_collector_thread: threading.Thread | None = None
_shutdown_event = threading.Event()
_native_events_buffer: list[tuple[str, str, ArtifactRef]] = []
_native_lock = threading.Lock()
_native_child_task_ids: dict[int, str] = {}
_native_child_task_lock = threading.Lock()
_native_thread_task_ids: dict[int, str] = {}
_recent_native_thread_task_ids: dict[int, tuple[str, float]] = {}
_native_thread_task_lock = threading.Lock()
_native_task_launch_context = threading.local()
_native_threading_patch_lock = threading.Lock()
_native_threading_patch_refcount = 0
_task_timing_state: dict[str, dict[str, Any]] = {}
_task_timing_lock = threading.Lock()
_direct_streamer: GlaasFragmentStreamer | None = None
_direct_streamer_lock = threading.Lock()
_s3_tracking_scope = threading.local()

_FLUSH_INTERVAL_SECONDS = float(os.environ.get("ROAR_FRAGMENT_FLUSH_INTERVAL", "2.0"))
_IDLE_FLUSH_INTERVAL_SECONDS = float(os.environ.get("ROAR_FRAGMENT_IDLE_FLUSH_INTERVAL", "0.25"))
_FLUSH_THRESHOLD_EVENTS = int(os.environ.get("ROAR_FRAGMENT_FLUSH_THRESHOLD", "200"))
_TASK_BOUNDARY_NATIVE_FLUSH_WAIT_SECONDS = float(
    os.environ.get("ROAR_RAY_TASK_NATIVE_FLUSH_WAIT", "0.2")
)
_TASK_BOUNDARY_NATIVE_QUIET_PERIOD_SECONDS = float(
    os.environ.get("ROAR_RAY_TASK_NATIVE_FLUSH_QUIET", "0.02")
)
_TASK_BOUNDARY_NATIVE_POLL_INTERVAL_SECONDS = float(
    os.environ.get("ROAR_RAY_TASK_NATIVE_FLUSH_POLL", "0.01")
)
_RECENT_NATIVE_THREAD_BINDING_LINGER_SECONDS = float(
    os.environ.get("ROAR_RAY_NATIVE_THREAD_BINDING_LINGER", "1.0")
)

_PROXY_LOG_RE = re.compile(
    r"^\[S3:(\w+)\]\s+(s3://[^\s]+)"
    r"(?:\s+\((\d+)\s+bytes\))?"
    r"(?:\s+etag=(\S+))?"
)
_S3_WRITE_OPS = frozenset({"PutObject", "UploadPart", "CompleteMultipartUpload", "DeleteObject"})

_real_open = builtins.open

_Blake3Constructor = Callable[[], Any]

try:
    from blake3 import blake3 as _blake3_import
except Exception:
    _blake3_constructor: _Blake3Constructor | None = None
else:
    _blake3_constructor = _blake3_import

_startup_complete = False
_actor_attribution_mode = "per_call"
_proxy_configured = False
_real_subprocess_popen = subprocess.Popen
_real_thread_start = threading.Thread.start


def _get_logger():
    from roar.core.logging import get_logger

    return get_logger()


def _active_hash_algorithm() -> str:
    return "blake3" if _blake3_constructor is not None else "sha256"


def _new_stream_hasher():
    if _blake3_constructor is not None:
        return _blake3_constructor()
    return hashlib.sha256()


def _to_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        try:
            return value.hex()
        except Exception:
            return value.decode("utf-8", errors="ignore")
    text = str(value)
    return text or None


def _get_current_task_id() -> str:
    try:
        ray = sys.modules.get("ray")
        if ray is None:
            return ""
        ctx = ray.get_runtime_context()
        task_id = ctx.get_task_id()
        return _to_text(task_id) or ""
    except Exception:
        return ""


def _resolved_task_id() -> str:
    task_id = _get_current_task_id()
    if task_id:
        return task_id

    bound_task_id = _bound_native_task_id_for_event(os.getpid(), threading.get_native_id())
    if bound_task_id:
        return bound_task_id

    return _current_native_launch_task_id()


def _get_actor_id() -> str | None:
    try:
        ray = sys.modules.get("ray")
        if ray is None:
            return None
        ctx = ray.get_runtime_context()
        actor_id = ctx.get_actor_id()
        return _to_text(actor_id)
    except Exception:
        return None


def _get_node_id() -> str | None:
    try:
        ray = sys.modules.get("ray")
        if ray is None:
            return None
        ctx = ray.get_runtime_context()
        node_id = ctx.get_node_id()
        return _to_text(node_id)
    except Exception:
        return None


def _get_worker_id() -> str | None:
    try:
        ray = sys.modules.get("ray")
        if ray is None:
            return None
        worker = getattr(ray, "_private", None)
        worker = getattr(worker, "worker", None)
        global_worker = getattr(worker, "global_worker", None)
        worker_id = getattr(global_worker, "worker_id", None)
        return _to_text(worker_id)
    except Exception:
        return None


def _get_actor_attribution() -> str:
    default = "per_call"
    try:
        start_dir = os.environ.get("ROAR_PROJECT_DIR") or os.getcwd()
        ray_config = load_ray_backend_config(start_dir=start_dir)
        configured = str(ray_config.get("actor_attribution", default)).strip().lower()
        if configured in {"per_call", "per_actor"}:
            return configured
    except Exception:
        pass
    return default


def _get_task_function_name() -> str:
    def _resolve_name(candidate: Any) -> str:
        try:
            value = candidate() if callable(candidate) else candidate
        except Exception:
            return ""
        text = _to_text(value) or ""
        return "" if text == "unknown" else text

    try:
        ray = sys.modules.get("ray")
        if ray is None:
            return "unknown"
        ctx = ray.get_runtime_context()
        for attr in ("get_task_function_name", "get_task_name"):
            name = _resolve_name(getattr(ctx, attr, None))
            if name:
                return name

        worker = getattr(ray, "_private", None)
        worker = getattr(worker, "worker", None)
        global_worker = getattr(worker, "global_worker", None)
        for attr in ("current_task_function_name", "current_task_name"):
            name = _resolve_name(getattr(global_worker, attr, None))
            if name:
                return name
    except Exception:
        pass
    return "unknown"


def _start_fragment(task_id: str, function_name: str = "") -> TaskFragment:
    now = time.time()
    roar_job_id = str(os.environ.get("ROAR_JOB_ID", "default"))
    started_at = _task_started_at(task_id) or now
    resolved_function_name = (
        function_name or _task_function_name(task_id) or _get_task_function_name()
    )
    return TaskFragment(
        job_uid=derive_task_uid(roar_job_id, task_id),
        parent_job_uid=str(os.environ.get("ROAR_DRIVER_JOB_UID", "")),
        ray_task_id=task_id,
        ray_worker_id=_get_worker_id() or "",
        ray_node_id=_get_node_id() or "",
        ray_actor_id=_get_actor_id(),
        function_name=resolved_function_name,
        started_at=started_at,
        ended_at=now,
        exit_code=0,
    )


def _register_task_timing(task_id: str, function_name: str) -> None:
    if not task_id:
        return
    with _task_timing_lock:
        _task_timing_state[task_id] = {
            "started_at": time.time(),
            "function_name": function_name or "",
            "lineage_observed": False,
        }


def _mark_task_lineage_observed(task_id: str, function_name: str = "") -> None:
    if not task_id:
        return
    with _task_timing_lock:
        state = _task_timing_state.get(task_id)
        if state is None:
            state = {
                "started_at": time.time(),
                "function_name": function_name or "",
                "lineage_observed": True,
            }
            _task_timing_state[task_id] = state
            return
        state["lineage_observed"] = True
        if function_name and not state.get("function_name"):
            state["function_name"] = function_name


def _task_started_at(task_id: str) -> float | None:
    if not task_id:
        return None
    with _task_timing_lock:
        state = _task_timing_state.get(task_id)
    if not isinstance(state, dict):
        return None
    started_at = state.get("started_at")
    if isinstance(started_at, (int, float)):
        return float(started_at)
    return None


def _task_function_name(task_id: str) -> str:
    if not task_id:
        return ""
    with _task_timing_lock:
        state = _task_timing_state.get(task_id)
    if not isinstance(state, dict):
        return ""
    function_name = state.get("function_name")
    return function_name if isinstance(function_name, str) else ""


def _emit_fragment(fragment: TaskFragment) -> None:
    emit_fragment_dicts(
        [fragment.to_dict()],
        env=os.environ,
        local_merge=lambda fragments, project_dir, driver_job_uid: collect_fragments(
            fragments=fragments,
            project_dir=project_dir,
            driver_job_uid=driver_job_uid,
        ),
    )


def _emit_task_timing_fragment(task_id: str, *, function_name: str, exit_code: int) -> None:
    if not task_id:
        return
    with _task_timing_lock:
        state = _task_timing_state.pop(task_id, None)
    if not isinstance(state, dict) or not state.get("lineage_observed"):
        return

    started_at = state.get("started_at")
    if not isinstance(started_at, (int, float)):
        return

    fragment = _start_fragment(task_id, function_name or str(state.get("function_name") or ""))
    fragment.started_at = float(started_at)
    fragment.ended_at = time.time()
    fragment.exit_code = exit_code
    _emit_fragment(fragment)


def _append_fragment_ref(fragment: TaskFragment, kind: str, ref: ArtifactRef) -> None:
    if kind == "write":
        fragment.writes.append(ref)
        return
    fragment.reads.append(ref)


def _emit_local_event_immediately(event: IOEvent) -> None:
    streamer_instance = _ensure_direct_streamer()
    if streamer_instance is None:
        return

    fragment = _start_fragment(event.task_id, event.function_name)
    ref = ArtifactRef(
        path=event.path,
        hash=event.hash_value,
        hash_algorithm=event.hash_algorithm,
        size=event.size,
        capture_method=event.capture_method,
    )
    _append_fragment_ref(fragment, event.kind, ref)
    fragment.ended_at = time.time()

    with _direct_streamer_lock:
        try:
            streamer_instance.append_fragment(fragment.to_dict())
            if not streamer_instance.flush():
                _get_logger().warning(
                    "Failed to eagerly flush Ray local event for task %s",
                    fragment.ray_task_id,
                )
        except Exception as exc:
            _get_logger().warning("Failed to eagerly append Ray local event: %s", exc)


def _emit_native_entries_immediately(task_id: str, entries: list[tuple[str, ArtifactRef]]) -> None:
    if not task_id or not entries:
        return

    _mark_task_lineage_observed(task_id)
    streamer_instance = _ensure_direct_streamer()
    if streamer_instance is None:
        return

    fragment = _start_fragment(task_id)
    for kind, ref in entries:
        _append_fragment_ref(fragment, kind, ref)
    fragment.ended_at = time.time()

    with _direct_streamer_lock:
        try:
            streamer_instance.append_fragment(fragment.to_dict())
            if not streamer_instance.flush():
                _get_logger().warning(
                    "Failed to eagerly flush Ray native events for task %s",
                    fragment.ray_task_id,
                )
        except Exception as exc:
            _get_logger().warning("Failed to eagerly append Ray native events: %s", exc)


def _drain_native_tracer_events() -> list[tuple[str, str, ArtifactRef]]:
    """Drain buffered native tracer events. Called by collector thread."""
    with _native_lock:
        events = list(_native_events_buffer)
        _native_events_buffer.clear()
    return events


def _register_native_child_pid(pid: int | None, task_id: str) -> None:
    if not task_id or not isinstance(pid, int) or pid <= 0:
        return
    with _native_child_task_lock:
        _native_child_task_ids[pid] = task_id


def _unregister_native_child_pid(pid: int | None) -> None:
    if not isinstance(pid, int) or pid <= 0:
        return
    with _native_child_task_lock:
        _native_child_task_ids.pop(pid, None)


def _register_native_thread_task(thread_id: int | None, task_id: str) -> None:
    if not task_id or not isinstance(thread_id, int) or thread_id <= 0:
        return
    with _native_thread_task_lock:
        _native_thread_task_ids[thread_id] = task_id
        _recent_native_thread_task_ids.pop(thread_id, None)


def _unregister_native_thread_task(thread_id: int | None, task_id: str | None = None) -> None:
    if not isinstance(thread_id, int) or thread_id <= 0:
        return
    with _native_thread_task_lock:
        current = _native_thread_task_ids.get(thread_id)
        if current is None:
            return
        if task_id is not None and current != task_id:
            return
        _native_thread_task_ids.pop(thread_id, None)
        _recent_native_thread_task_ids[thread_id] = (
            current,
            time.monotonic() + _RECENT_NATIVE_THREAD_BINDING_LINGER_SECONDS,
        )


def _recent_native_thread_task_id(thread_id: int) -> str:
    with _native_thread_task_lock:
        recent = _recent_native_thread_task_ids.get(thread_id)
        if recent is None:
            return ""
        task_id, expires_at = recent
        if time.monotonic() <= expires_at:
            return task_id
        _recent_native_thread_task_ids.pop(thread_id, None)
        return ""


def _bound_native_task_id_for_event(pid: int | None, thread_id: int | None) -> str:
    if not isinstance(pid, int) or pid <= 0:
        pid = None
    if pid is not None and pid != os.getpid():
        with _native_child_task_lock:
            return _native_child_task_ids.get(pid, "")
    if isinstance(thread_id, int) and thread_id > 0:
        with _native_thread_task_lock:
            current = _native_thread_task_ids.get(thread_id, "")
        if current:
            return current
        return _recent_native_thread_task_id(thread_id)
    with _native_child_task_lock:
        return _native_child_task_ids.get(pid, "") if pid is not None else ""


def _current_native_launch_task_id() -> str:
    task_id = getattr(_native_task_launch_context, "task_id", "")
    return task_id if isinstance(task_id, str) else ""


def _flush_current_task_native_events_immediately() -> None:
    task_id = _get_current_task_id()
    if not task_id:
        return

    deadline = time.monotonic() + _TASK_BOUNDARY_NATIVE_FLUSH_WAIT_SECONDS
    quiet_deadline: float | None = None
    pending_by_task: dict[str, list[tuple[str, ArtifactRef]]] = {}

    while True:
        batch = _drain_native_tracer_events()
        if batch:
            for bound_task_id, kind, ref in batch:
                resolved_task_id = bound_task_id or task_id
                if not resolved_task_id:
                    continue
                pending_by_task.setdefault(resolved_task_id, []).append((kind, ref))
            quiet_deadline = time.monotonic() + _TASK_BOUNDARY_NATIVE_QUIET_PERIOD_SECONDS

        now = time.monotonic()
        if quiet_deadline is not None:
            if now >= quiet_deadline or now >= deadline:
                break
        elif now >= deadline:
            break

        remaining = deadline - now
        if remaining <= 0:
            break
        time.sleep(min(_TASK_BOUNDARY_NATIVE_POLL_INTERVAL_SECONDS, remaining))

    for resolved_task_id, entries in pending_by_task.items():
        _emit_native_entries_immediately(resolved_task_id, entries)


def _patch_subprocess_for_native_task_attribution() -> None:
    current_popen = getattr(subprocess, "Popen", None)
    if not isinstance(current_popen, type):
        return
    if getattr(current_popen, "_roar_patched", False):
        return

    class _TrackedPopen(_real_subprocess_popen):  # type: ignore[misc, valid-type]
        _roar_patched = True

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            _register_native_child_pid(self.pid, _current_native_launch_task_id())

        def _roar_maybe_unregister(self, result: int | None = None) -> None:
            if result is not None or self.returncode is not None:
                _unregister_native_child_pid(self.pid)

        def poll(self):
            result = super().poll()
            self._roar_maybe_unregister(result)
            return result

        def wait(self, timeout=None):
            result = super().wait(timeout=timeout)
            self._roar_maybe_unregister(result)
            return result

        def communicate(self, input=None, timeout=None):
            result = super().communicate(input=input, timeout=timeout)
            self._roar_maybe_unregister(0 if self.returncode is not None else None)
            return result

        def __exit__(self, exc_type, exc_val, exc_tb):
            try:
                return super().__exit__(exc_type, exc_val, exc_tb)
            finally:
                self._roar_maybe_unregister(0 if self.returncode is not None else None)

    subprocess.Popen = _TrackedPopen  # type: ignore[misc]


def _roar_thread_start(self, *args, **kwargs):
    if not getattr(self, "_roar_native_thread_wrapped", False):
        task_id = _current_native_launch_task_id()
        if task_id:
            original_run = self.run

            @functools.wraps(original_run)
            def _roar_run(*run_args, **run_kwargs):
                thread_id = threading.get_native_id()
                _register_native_thread_task(thread_id, task_id)
                try:
                    return original_run(*run_args, **run_kwargs)
                finally:
                    _unregister_native_thread_task(thread_id, task_id)

            self.run = _roar_run  # type: ignore[method-assign]
            self._roar_native_thread_wrapped = True  # type: ignore[attr-defined]

    return _real_thread_start(self, *args, **kwargs)


def _activate_threading_patch_for_native_task_attribution() -> None:
    global _native_threading_patch_refcount

    with _native_threading_patch_lock:
        if _native_threading_patch_refcount == 0:
            threading.Thread.start = _roar_thread_start  # type: ignore[method-assign]
        _native_threading_patch_refcount += 1


def _deactivate_threading_patch_for_native_task_attribution() -> None:
    global _native_threading_patch_refcount

    with _native_threading_patch_lock:
        if _native_threading_patch_refcount <= 0:
            _native_threading_patch_refcount = 0
            threading.Thread.start = _real_thread_start  # type: ignore[method-assign]
            return

        _native_threading_patch_refcount -= 1
        if _native_threading_patch_refcount == 0:
            threading.Thread.start = _real_thread_start  # type: ignore[method-assign]


def _warn_task_capture_unavailable(reason: str) -> None:
    try:
        import ray

        version = getattr(ray, "__version__", "unknown")
    except Exception:
        version = "unknown"
    print(
        f"[roar] warning: per-task lineage capture is unavailable on ray {version} "
        f"({reason}); task fragments may lack identity and file refs",
        file=sys.stderr,
    )


def _wrap_task_executor_for_native_flush(
    function: Callable[..., Any],
    *,
    function_name: str = "",
) -> Callable[..., Any]:
    if getattr(function, "_roar_native_flush_wrapped", False):
        return function

    @functools.wraps(function)
    def _wrapped(*args, **kwargs):
        task_id = _get_current_task_id()
        resolved_function_name = function_name or _get_task_function_name()
        thread_id = threading.get_native_id()
        previous_launch_task_id = _current_native_launch_task_id()
        _native_task_launch_context.task_id = task_id
        _activate_threading_patch_for_native_task_attribution()
        _register_native_thread_task(thread_id, task_id)
        _register_task_timing(task_id, resolved_function_name)
        exit_code = 0
        try:
            return function(*args, **kwargs)
        except Exception:
            exit_code = 1
            raise
        finally:
            _deactivate_threading_patch_for_native_task_attribution()
            if previous_launch_task_id:
                _native_task_launch_context.task_id = previous_launch_task_id
            else:
                with contextlib.suppress(AttributeError):
                    delattr(_native_task_launch_context, "task_id")
            with contextlib.suppress(Exception):
                _flush_current_task_native_events_immediately()
            with contextlib.suppress(Exception):
                _emit_task_timing_fragment(
                    task_id,
                    function_name=resolved_function_name,
                    exit_code=exit_code,
                )
            _unregister_native_thread_task(thread_id, task_id)

    cast(Any, _wrapped)._roar_native_flush_wrapped = True
    for attr in ("name", "method"):
        if hasattr(function, attr):
            with contextlib.suppress(Exception):
                setattr(_wrapped, attr, getattr(function, attr))
    return _wrapped


def _patch_ray_task_execution_for_native_flush() -> None:
    try:
        from ray._private.function_manager import FunctionActorManager, FunctionExecutionInfo
    except Exception as exc:
        _warn_task_capture_unavailable(f"cannot import ray function manager: {exc}")
        return

    if not callable(getattr(FunctionActorManager, "get_execution_info", None)) and not callable(
        getattr(FunctionActorManager, "_make_actor_method_executor", None)
    ):
        # A future Ray moved the executor internals: task-boundary capture
        # cannot engage, and task fragments would silently arrive without
        # per-task identity or file refs. Fail loudly instead
        # (verified engaging on 2.46 and 2.54).
        _warn_task_capture_unavailable(
            "FunctionActorManager has neither get_execution_info nor _make_actor_method_executor"
        )
        return

    current_get_execution_info = getattr(FunctionActorManager, "get_execution_info", None)
    if callable(current_get_execution_info) and not getattr(
        current_get_execution_info, "_roar_patched", False
    ):

        def _roar_get_execution_info(self, job_id, function_descriptor):
            info = current_get_execution_info(self, job_id, function_descriptor)
            wrapped_function = _wrap_task_executor_for_native_flush(
                info.function,
                function_name=str(info.function_name or ""),
            )
            if wrapped_function is info.function:
                return info

            wrapped_info = FunctionExecutionInfo(
                function=wrapped_function,
                function_name=info.function_name,
                max_calls=info.max_calls,
            )

            function_id = getattr(function_descriptor, "function_id", None)
            if function_id is not None:
                with contextlib.suppress(Exception):
                    self._function_execution_info[function_id] = wrapped_info
            return wrapped_info

        _roar_get_execution_info._roar_patched = True  # type: ignore[attr-defined]
        FunctionActorManager.get_execution_info = _roar_get_execution_info  # type: ignore[method-assign]

    current_make_actor_method_executor = getattr(
        FunctionActorManager,
        "_make_actor_method_executor",
        None,
    )
    if callable(current_make_actor_method_executor) and not getattr(
        current_make_actor_method_executor, "_roar_patched", False
    ):

        def _roar_make_actor_method_executor(self, method_name, method):
            wrapped_method = _wrap_task_executor_for_native_flush(
                method,
                function_name=str(method_name or ""),
            )
            return current_make_actor_method_executor(self, method_name, wrapped_method)

        _roar_make_actor_method_executor._roar_patched = True  # type: ignore[attr-defined]
        FunctionActorManager._make_actor_method_executor = _roar_make_actor_method_executor  # type: ignore[method-assign]


def _start_native_tracer_socket() -> None:
    """Bind to the Unix socket path set by roar_worker_wrapper.sh.

    The wrapper script creates a temp directory and sets
    ROAR_PRELOAD_TRACE_SOCK *before* exec-ing Python so that the
    LD_PRELOAD .so caches the path on its first libc interposition.
    We bind to that same path here — the .so will connect (or reconnect)
    on its next I/O call.

    If the env var isn't set (e.g. no wrapper), we create our own path,
    but the .so won't find it unless it hasn't cached yet.
    """
    sock_path = os.environ.get("ROAR_PRELOAD_TRACE_SOCK")
    if sock_path:
        sock_dir = os.path.dirname(sock_path)
    else:
        sock_dir = tempfile.mkdtemp(prefix="roar-trace-")
        sock_path = os.path.join(sock_dir, "trace.sock")
        os.environ["ROAR_PRELOAD_TRACE_SOCK"] = sock_path

    # Remove stale socket file if it exists (e.g. from a previous run)
    with contextlib.suppress(FileNotFoundError):
        os.unlink(sock_path)

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)
    server.listen(8)
    server.settimeout(1.0)

    with contextlib.suppress(Exception):
        server.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)

    def _listener() -> None:
        while not _shutdown_event.is_set():
            try:
                conn, _ = server.accept()
                threading.Thread(
                    target=_handle_preload_connection,
                    args=(conn,),
                    name="roar-preload-conn",
                    daemon=True,
                ).start()
            except TimeoutError:
                continue
            except OSError:
                break
        server.close()
        try:
            os.unlink(sock_path)
            os.rmdir(sock_dir)
        except OSError:
            pass

    threading.Thread(
        target=_listener,
        name="roar-preload-listener",
        daemon=True,
    ).start()


def _handle_preload_connection(conn: socket.socket) -> None:
    """Read length-prefixed msgpack TraceEvent frames from one .so connection."""
    conn.settimeout(1.0)
    buf = bytearray()
    try:
        while not _shutdown_event.is_set():
            try:
                data = conn.recv(65536)
                if not data:
                    break
                buf.extend(data)
                _parse_and_buffer_frames(buf)
            except TimeoutError:
                continue
            except OSError:
                break
    finally:
        _parse_and_buffer_frames(buf)
        conn.close()


def _parse_and_buffer_frames(buf: bytearray) -> None:
    """Extract complete frames from buffer, convert to ArtifactRef, buffer them."""
    try:
        import msgpack
    except ImportError:
        return

    while len(buf) >= 4:
        length = struct.unpack_from("<I", buf, 0)[0]
        if len(buf) < 4 + length:
            break
        payload = buf[4 : 4 + length]
        del buf[: 4 + length]
        try:
            event = msgpack.unpackb(payload, raw=False)
        except Exception:
            continue
        if not isinstance(event, dict):
            continue

        kind_str = event.get("kind", "")
        path = event.get("path", "")
        if not isinstance(path, str) or not path or not _should_track_local_path(path):
            continue
        pid_value = event.get("pid")
        pid = pid_value if isinstance(pid_value, int) else None
        thread_id_value = event.get("thread_id")
        thread_id = thread_id_value if isinstance(thread_id_value, int) else None

        kind = "write" if kind_str == "write" else "read"
        ref = ArtifactRef(
            path=path,
            hash=None,
            hash_algorithm="",
            size=0,
            capture_method="native",
        )
        with _native_lock:
            _native_events_buffer.append(
                (_bound_native_task_id_for_event(pid, thread_id), kind, ref)
            )


def _parse_proxy_log_lines(lines: list[str]) -> list[tuple[str, ArtifactRef]]:
    """Parse proxy log lines into (kind, ArtifactRef) pairs."""
    results: list[tuple[str, ArtifactRef]] = []
    for line in lines:
        match = _PROXY_LOG_RE.match(line)
        if not match:
            continue
        op, s3_uri, size_str, etag = match.groups()
        if op in ("CreateMultipartUpload", "Other"):
            continue
        kind = "write" if op in _S3_WRITE_OPS else "read"
        ref = ArtifactRef(
            path=s3_uri,
            hash=etag.strip('"') if etag else None,
            hash_algorithm="etag" if etag else "",
            size=int(size_str) if size_str else 0,
            capture_method="proxy",
        )
        results.append((kind, ref))
    return results


def _is_loopback_proxy_endpoint(url: str) -> bool:
    text = str(url).strip().lower()
    return text.startswith("http://127.0.0.1:") or text.startswith("http://localhost:")


def _local_proxy_port() -> int:
    return local_proxy_port_from_env(os.environ)


def _ensure_direct_streamer() -> GlaasFragmentStreamer | None:
    global _direct_streamer

    if _direct_streamer is not None:
        return _direct_streamer

    session_id = os.environ.get("ROAR_SESSION_ID")
    token = os.environ.get("ROAR_FRAGMENT_TOKEN")
    glaas_url = os.environ.get("GLAAS_URL")
    if not (session_id and token and glaas_url):
        return None

    with _direct_streamer_lock:
        if _direct_streamer is not None:
            return _direct_streamer

        try:
            _direct_streamer = GlaasFragmentStreamer(
                session_id=session_id,
                token=token,
                glaas_url=glaas_url,
            )
        except Exception as exc:
            _get_logger().warning(
                "Failed to initialize direct Ray fragment streamer for session %s: %s",
                session_id,
                exc,
            )
            return None

    return _direct_streamer


def _start_collector() -> None:
    global _collector_thread

    if _collector_thread is not None and _collector_thread.is_alive():
        return

    _shutdown_event.clear()

    def _collector_loop() -> None:
        fragment: TaskFragment | None = None
        events_since_flush = 0
        last_flush = time.monotonic()
        last_activity = last_flush

        def _ensure_streamer() -> GlaasFragmentStreamer | None:
            return _ensure_direct_streamer()

        def _flush_fragment_batch(*, continuation: bool) -> None:
            nonlocal events_since_flush, fragment, last_flush

            if fragment is None or not (fragment.reads or fragment.writes):
                if continuation and fragment is not None:
                    fragment = _start_fragment(fragment.ray_task_id)
                    last_flush = time.monotonic()
                return

            current_task_id = fragment.ray_task_id
            fragment.ended_at = time.time()
            streamer_instance = _ensure_streamer()
            if streamer_instance is not None:
                with _direct_streamer_lock:
                    try:
                        streamer_instance.append_fragment(fragment.to_dict())
                        if not streamer_instance.flush():
                            _get_logger().warning(
                                "Failed to flush Ray fragment batch for task %s",
                                fragment.ray_task_id,
                            )
                    except Exception as exc:
                        _get_logger().warning("Failed to append Ray fragment: %s", exc)

            fragment = _start_fragment(current_task_id) if continuation else None
            events_since_flush = 0
            last_flush = time.monotonic()

        def _ensure_fragment(task_id: str, function_name: str = "") -> TaskFragment:
            nonlocal fragment, events_since_flush, last_flush

            normalized_task_id = task_id or ""
            if fragment is None:
                fragment = _start_fragment(normalized_task_id, function_name)
                events_since_flush = 0
                last_flush = time.monotonic()
            elif normalized_task_id != fragment.ray_task_id:
                _flush_fragment_batch(continuation=False)
                fragment = _start_fragment(normalized_task_id, function_name)
                events_since_flush = 0
                last_flush = time.monotonic()
            return fragment

        def _process_event(event: IOEvent) -> None:
            nonlocal events_since_flush, last_activity

            _mark_task_lineage_observed(event.task_id, event.function_name)
            current_fragment = _ensure_fragment(event.task_id, event.function_name)
            ref = ArtifactRef(
                path=event.path,
                hash=event.hash_value,
                hash_algorithm=event.hash_algorithm,
                size=event.size,
                capture_method=event.capture_method,
            )
            _append_fragment_ref(current_fragment, event.kind, ref)
            events_since_flush += 1
            last_activity = time.monotonic()

        def _process_native_entries(entries: list[tuple[str, str, ArtifactRef]]) -> None:
            nonlocal events_since_flush, last_activity

            if not entries:
                return

            current_task_id = _get_current_task_id()
            for bound_task_id, kind, ref in entries:
                resolved_task_id = bound_task_id or current_task_id
                _mark_task_lineage_observed(resolved_task_id)
                current_fragment = _ensure_fragment(resolved_task_id)
                _append_fragment_ref(current_fragment, kind, ref)
                events_since_flush += 1
            last_activity = time.monotonic()

        while True:
            if _shutdown_event.is_set():
                try:
                    event = _event_queue.get_nowait()
                except queue.Empty:
                    break
            else:
                try:
                    event = _event_queue.get(timeout=0.1)
                except queue.Empty:
                    event = None

            if event is not None:
                _process_event(event)
                while True:
                    try:
                        queued_event = _event_queue.get_nowait()
                    except queue.Empty:
                        break
                    if queued_event is None:
                        _shutdown_event.set()
                        break
                    _process_event(queued_event)
            elif _shutdown_event.is_set():
                break

            native_entries = _drain_native_tracer_events()
            if native_entries:
                _process_native_entries(native_entries)

            now = time.monotonic()
            if events_since_flush > 0 and (
                events_since_flush >= _FLUSH_THRESHOLD_EVENTS
                or now - last_flush >= _FLUSH_INTERVAL_SECONDS
                or now - last_activity >= _IDLE_FLUSH_INTERVAL_SECONDS
            ):
                _flush_fragment_batch(continuation=True)

        while True:
            try:
                queued_event = _event_queue.get_nowait()
            except queue.Empty:
                break
            if queued_event is None:
                continue
            _process_event(queued_event)

        _process_native_entries(_drain_native_tracer_events())
        _flush_fragment_batch(continuation=False)
        _close_direct_streamer()

    _collector_thread = threading.Thread(
        target=_collector_loop,
        name="roar-fragment-collector",
        daemon=True,
    )
    _collector_thread.start()


def _shutdown_collector() -> None:
    _shutdown_event.set()
    _event_queue.put(None)
    if _collector_thread and _collector_thread.is_alive():
        _collector_thread.join(timeout=10)
    _close_direct_streamer()


def _close_direct_streamer() -> None:
    global _direct_streamer

    with _direct_streamer_lock:
        if _direct_streamer is None:
            return
        try:
            _direct_streamer.close()
        except Exception as exc:
            _get_logger().warning("Failed to close direct Ray fragment streamer: %s", exc)
        finally:
            _direct_streamer = None


def _is_write_mode(mode: str) -> bool:
    return any(flag in mode for flag in ("w", "a", "x", "+"))


def _should_track_local_path(path: str) -> bool:
    normalized = os.path.abspath(path)
    return not normalized.startswith(("/proc/", "/sys/", "/dev/"))


def _log_read(
    *,
    path: str,
    hash_value: str | None,
    hash_algorithm: str,
    size: int,
    capture_method: str,
    task_id: str | None = None,
    function_name: str | None = None,
) -> None:
    resolved_task_id = task_id if task_id is not None else _resolved_task_id()
    resolved_function_name = (
        function_name if function_name is not None else _get_task_function_name()
    )
    event = IOEvent(
        "read",
        resolved_task_id,
        resolved_function_name,
        path,
        hash_value,
        hash_algorithm,
        size,
        capture_method,
    )
    _mark_task_lineage_observed(resolved_task_id, resolved_function_name)
    _event_queue.put(event)
    _emit_local_event_immediately(event)


def _log_write(
    *,
    path: str,
    hash_value: str | None,
    hash_algorithm: str,
    size: int,
    capture_method: str,
    task_id: str | None = None,
    function_name: str | None = None,
) -> None:
    resolved_task_id = task_id if task_id is not None else _resolved_task_id()
    resolved_function_name = (
        function_name if function_name is not None else _get_task_function_name()
    )
    event = IOEvent(
        "write",
        resolved_task_id,
        resolved_function_name,
        path,
        hash_value,
        hash_algorithm,
        size,
        capture_method,
    )
    _mark_task_lineage_observed(resolved_task_id, resolved_function_name)
    _event_queue.put(event)
    _emit_local_event_immediately(event)


class _TrackedWriteFile:
    def __init__(self, handle, path: str, capture_method: str = "python") -> None:
        self._handle = handle
        self._path = path
        self._capture_method = capture_method
        self._hasher = _new_stream_hasher()
        self._size = 0
        self._closed = False

    def _update_hasher(self, data: Any) -> None:
        if isinstance(data, str):
            payload = data.encode("utf-8")
        elif isinstance(data, (bytes, bytearray, memoryview)):
            payload = bytes(data)
        else:
            return

        self._size += len(payload)
        if self._hasher is not None:
            self._hasher.update(payload)

    def write(self, data):
        result = self._handle.write(data)
        self._update_hasher(data)
        return result

    def writelines(self, lines):
        for line in lines:
            self._update_hasher(line)
        return self._handle.writelines(lines)

    def close(self) -> None:
        if self._closed:
            return

        hash_value: str | None = None
        if self._hasher is not None:
            try:
                hash_value = self._hasher.hexdigest()
            except Exception:
                hash_value = None

        self._handle.close()
        _log_write(
            path=self._path,
            hash_value=hash_value,
            hash_algorithm=_active_hash_algorithm(),
            size=self._size,
            capture_method=self._capture_method,
        )
        self._closed = True

    def flush(self) -> None:
        self._handle.flush()

    def __enter__(self):
        self._handle.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
        return self._handle.__exit__(exc_type, exc, tb)

    @property
    def closed(self) -> bool:
        return self._closed or bool(getattr(self._handle, "closed", False))

    def __getattr__(self, name: str):
        return getattr(self._handle, name)


def _tracking_open(*args, **kwargs):
    if _startup_complete and not _proxy_configured:
        _configure_local_proxy_endpoint()

    handle = _real_open(*args, **kwargs)

    raw_path = args[0] if args else kwargs.get("file")
    mode = args[1] if len(args) > 1 else kwargs.get("mode", "r")

    if isinstance(raw_path, (str, bytes, os.PathLike)):
        path = os.path.abspath(os.fspath(raw_path))
        if not _should_track_local_path(path):
            return handle
        if _is_write_mode(str(mode)):
            return _TrackedWriteFile(handle, path=path, capture_method="python")

        _log_read(
            path=path,
            hash_value=None,
            hash_algorithm=_active_hash_algorithm(),
            size=0,
            capture_method="python",
        )

    return handle


def _patch_pandas_parquet() -> None:
    try:
        import pandas as pd
    except Exception:
        return

    original_to_parquet = getattr(pd.DataFrame, "to_parquet", None)
    if not callable(original_to_parquet):
        return
    if getattr(original_to_parquet, "_roar_worker_patched", False):
        return

    def _tracked_to_parquet(self, path, *args, **kwargs):
        result = original_to_parquet(self, path, *args, **kwargs)
        try:
            if isinstance(path, (str, bytes, os.PathLike)):
                resolved = os.path.abspath(os.fspath(path))
                if _should_track_local_path(resolved):
                    _log_write(
                        path=resolved,
                        hash_value=None,
                        hash_algorithm=_active_hash_algorithm(),
                        size=0,
                        capture_method="tracer",
                    )
        except Exception:
            pass
        return result

    _tracked_to_parquet._roar_worker_patched = True  # type: ignore[attr-defined]
    pd.DataFrame.to_parquet = _tracked_to_parquet


def _patch_tempfile() -> None:
    if getattr(tempfile, "_roar_worker_tempfile_patched", False):
        return

    real_named_temporary_file = tempfile.NamedTemporaryFile

    def _tracked_named_temporary_file(*args, **kwargs):
        handle = real_named_temporary_file(*args, **kwargs)
        try:
            path = os.path.abspath(os.fspath(handle.name))
            if _should_track_local_path(path) and _is_write_mode(str(getattr(handle, "mode", ""))):
                return _TrackedWriteFile(handle, path=path, capture_method="python")
        except Exception:
            pass
        return handle

    tempfile.NamedTemporaryFile = _tracked_named_temporary_file
    tempfile._roar_worker_tempfile_patched = True  # type: ignore[attr-defined]


def _normalize_etag(value: Any) -> str | None:
    text = _to_text(value)
    if not text:
        return None
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = text[1:-1]
    return text or None


def _extract_bucket_key(
    args: tuple[Any, ...], kwargs: dict[str, Any]
) -> tuple[str | None, str | None]:
    bucket = kwargs.get("Bucket")
    key = kwargs.get("Key")
    if bucket and key:
        return _to_text(bucket), _to_text(key)
    if len(args) >= 2:
        return _to_text(args[0]), _to_text(args[1])
    return None, None


def _payload_size_bytes(body: Any) -> int:
    if body is None:
        return 0
    if isinstance(body, str):
        return len(body.encode("utf-8"))
    if isinstance(body, (bytes, bytearray, memoryview)):
        return len(body)

    try:
        length = len(body)  # type: ignore[arg-type]
    except Exception:
        length = None
    if isinstance(length, int) and length >= 0:
        return length

    tell = getattr(body, "tell", None)
    seek = getattr(body, "seek", None)
    if callable(tell) and callable(seek):
        try:
            current = int(tell())
            seek(0, os.SEEK_END)
            end = int(tell())
            seek(current, os.SEEK_SET)
            return max(0, end)
        except Exception:
            return 0

    return 0


def _response_size_bytes(response: Any) -> int:
    if not isinstance(response, dict):
        return 0
    for field in ("ContentLength", "Size", "content_length", "size"):
        value = response.get(field)
        if isinstance(value, int) and value >= 0:
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return 0


def _log_s3_write(*args, **kwargs) -> None:
    task_id = str(kwargs.pop("_roar_task_id", "") or "")
    function_name = str(kwargs.pop("_roar_function_name", "") or "")
    if not task_id:
        task_id = _resolved_task_id()
    if not function_name:
        function_name = _get_task_function_name()
    if not task_id:
        return

    bucket, key = _extract_bucket_key(args, kwargs)
    if not bucket or not key:
        return

    response = kwargs.pop("_roar_response", None)
    size = _payload_size_bytes(kwargs.get("Body"))
    etag = _normalize_etag(response.get("ETag")) if isinstance(response, dict) else None
    _log_write(
        path=f"s3://{bucket}/{key}",
        hash_value=etag,
        hash_algorithm="etag" if etag else "",
        size=size,
        capture_method="proxy",
        task_id=task_id,
        function_name=function_name,
    )


def _log_s3_read(*args, **kwargs) -> None:
    task_id = str(kwargs.pop("_roar_task_id", "") or "")
    function_name = str(kwargs.pop("_roar_function_name", "") or "")
    if not task_id:
        task_id = _resolved_task_id()
    if not function_name:
        function_name = _get_task_function_name()
    if not task_id:
        return

    bucket, key = _extract_bucket_key(args, kwargs)
    if not bucket or not key:
        return

    response = kwargs.pop("_roar_response", None)
    etag = _normalize_etag(response.get("ETag")) if isinstance(response, dict) else None
    _log_read(
        path=f"s3://{bucket}/{key}",
        hash_value=etag,
        hash_algorithm="etag" if etag else "",
        size=_response_size_bytes(response),
        capture_method="proxy",
        task_id=task_id,
        function_name=function_name,
    )


def _track_s3_api_call(
    operation_name: str,
    api_params: dict[str, Any],
    response: Any,
    *,
    task_id: str | None = None,
    function_name: str | None = None,
) -> None:
    resolved_task_id = task_id if task_id is not None else _resolved_task_id()
    resolved_function_name = (
        function_name if function_name is not None else _get_task_function_name()
    )
    if not resolved_task_id:
        return

    bucket = _to_text(api_params.get("Bucket"))
    key = _to_text(api_params.get("Key"))
    if not bucket or not key:
        return

    if operation_name in _S3_WRITE_OPS:
        _log_s3_write(
            Bucket=bucket,
            Key=key,
            Body=api_params.get("Body"),
            _roar_response=response,
            _roar_task_id=resolved_task_id,
            _roar_function_name=resolved_function_name,
        )
        return

    _log_s3_read(
        Bucket=bucket,
        Key=key,
        _roar_response=response,
        _roar_task_id=resolved_task_id,
        _roar_function_name=resolved_function_name,
    )


def _wrap_s3_client_method(client: Any, method_name: str, operation_name: str) -> None:
    real_method = getattr(client, method_name, None)
    if not callable(real_method) or getattr(real_method, "_roar_patched", False):
        return

    @functools.wraps(real_method)
    def _wrapped(*args, **kwargs):
        task_id = _resolved_task_id()
        function_name = _get_task_function_name()
        _s3_tracking_scope.active = True
        try:
            response = real_method(*args, **kwargs)
        finally:
            _s3_tracking_scope.active = False

        api_params = dict(kwargs) if isinstance(kwargs, dict) else {}
        if not api_params:
            bucket, key = _extract_bucket_key(args, kwargs)
            if bucket:
                api_params["Bucket"] = bucket
            if key:
                api_params["Key"] = key
        with contextlib.suppress(Exception):
            _track_s3_api_call(
                operation_name,
                api_params,
                response,
                task_id=task_id,
                function_name=function_name,
            )
        return response

    _wrapped._roar_patched = True  # type: ignore[attr-defined]
    setattr(client, method_name, _wrapped)


def _wrap_s3_client(client: Any) -> Any:
    if getattr(client, "_roar_s3_wrapped", False):
        return client

    method_operations = {
        "put_object": "PutObject",
        "get_object": "GetObject",
        "delete_object": "DeleteObject",
        "upload_part": "UploadPart",
        "complete_multipart_upload": "CompleteMultipartUpload",
    }
    for method_name, operation_name in method_operations.items():
        _wrap_s3_client_method(client, method_name, operation_name)

    client._roar_s3_wrapped = True
    return client


def _patch_boto3() -> None:
    try:
        import boto3
        from botocore.client import BaseClient
    except Exception:
        return

    real_make_api_call = getattr(BaseClient, "_make_api_call", None)
    if not callable(real_make_api_call) or getattr(real_make_api_call, "_roar_patched", False):
        return

    def _tracking_make_api_call(self, operation_name, api_params):
        response = real_make_api_call(self, operation_name, api_params)
        if getattr(_s3_tracking_scope, "active", False):
            return response
        with contextlib.suppress(Exception):
            meta = getattr(self, "meta", None)
            service_model = getattr(meta, "service_model", None)
            service_name = str(getattr(service_model, "service_name", "") or "").lower()
            if service_name == "s3":
                _track_s3_api_call(
                    str(operation_name or ""),
                    api_params if isinstance(api_params, dict) else {},
                    response,
                )
        return response

    _tracking_make_api_call._roar_patched = True  # type: ignore[attr-defined]
    BaseClient._make_api_call = _tracking_make_api_call  # type: ignore[method-assign]

    real_boto3_client = getattr(boto3, "client", None)
    if callable(real_boto3_client) and not getattr(real_boto3_client, "_roar_patched", False):

        @functools.wraps(real_boto3_client)
        def _tracking_boto3_client(*args, **kwargs):
            client = real_boto3_client(*args, **kwargs)
            service_name = ""
            if args:
                service_name = str(args[0] or "").lower()
            elif "service_name" in kwargs:
                service_name = str(kwargs.get("service_name") or "").lower()
            if service_name == "s3":
                return _wrap_s3_client(client)
            return client

        _tracking_boto3_client._roar_patched = True  # type: ignore[attr-defined]
        boto3.client = _tracking_boto3_client  # type: ignore[assignment]

    session_client = getattr(boto3.session.Session, "client", None)
    if callable(session_client) and not getattr(session_client, "_roar_patched", False):

        @functools.wraps(session_client)
        def _tracking_session_client(self, service_name, *args, **kwargs):
            client = session_client(self, service_name, *args, **kwargs)
            if str(service_name or "").lower() == "s3":
                return _wrap_s3_client(client)
            return client

        _tracking_session_client._roar_patched = True  # type: ignore[attr-defined]
        boto3.session.Session.client = _tracking_session_client  # type: ignore[assignment]


def _configure_local_proxy_endpoint() -> None:
    """Point S3 traffic at the local node proxy on the configured loopback port."""
    global _proxy_configured

    if _proxy_configured:
        return
    port = _local_proxy_port()

    # Configure the proxy endpoint.
    upstream = str(os.environ.get("ROAR_UPSTREAM_S3_ENDPOINT", "")).strip()
    original = str(os.environ.get("AWS_ENDPOINT_URL", "")).strip()
    if not upstream and original and not _is_loopback_proxy_endpoint(original):
        os.environ["ROAR_UPSTREAM_S3_ENDPOINT"] = original
    endpoint = local_proxy_endpoint(port)
    os.environ["AWS_ENDPOINT_URL"] = endpoint
    print(f"[roar-worker] set AWS_ENDPOINT_URL={endpoint}")

    os.environ["ROAR_PROXY_PORT"] = str(port)
    _proxy_configured = True
    print("[roar-worker] proxy endpoint configured")


def _resolve_preload_library_for_worker_exec() -> str | None:
    explicit = str(os.environ.get("ROAR_PRELOAD_LIB", "")).strip()
    if explicit and os.path.exists(explicit):
        return explicit

    for candidate_name in ("libroar_tracer_preload.so", "libroar-tracer-preload.so"):
        candidate = Path.cwd() / candidate_name
        if candidate.exists():
            return str(candidate.resolve())

    try:
        import roar
        from roar.execution.runtime.tracer_backends import find_preload_library

        roar_package_dir = Path(roar.__file__).resolve().parent
        library_path = find_preload_library(roar_package_dir)
    except Exception:
        return None

    if library_path and os.path.exists(library_path):
        return str(Path(library_path).resolve())
    return None


def _prepare_preload_env_for_worker_exec() -> None:
    library_path = _resolve_preload_library_for_worker_exec()
    if not library_path:
        return

    current_ld_preload = str(os.environ.get("LD_PRELOAD", "")).strip()
    current_entries = [entry for entry in re.split(r"[\s:]+", current_ld_preload) if entry]
    if library_path not in current_entries:
        current_entries.insert(0, library_path)
        os.environ["LD_PRELOAD"] = " ".join(current_entries)

    sock_path = str(os.environ.get("ROAR_PRELOAD_TRACE_SOCK", "")).strip()
    if not sock_path:
        sock_dir = tempfile.mkdtemp(prefix="roar-trace-")
        os.environ["ROAR_PRELOAD_TRACE_SOCK"] = os.path.join(sock_dir, "trace.sock")


def _startup() -> None:
    global _startup_complete, _actor_attribution_mode

    if _startup_complete:
        return

    _actor_attribution_mode = _get_actor_attribution()
    if "libroar_tracer_preload" in os.environ.get("LD_PRELOAD", ""):
        _start_native_tracer_socket()
    builtins.open = _tracking_open
    # pathlib (Path.open/read_bytes/write_bytes) and other stdlib callers
    # resolve `io.open` by module attribute, not the builtin — without the
    # preload tracer (e.g. KubeRay pods) those opens were invisible.
    io.open = _tracking_open  # type: ignore[assignment]
    _patch_subprocess_for_native_task_attribution()
    _patch_boto3()
    _patch_pandas_parquet()
    _patch_tempfile()
    _patch_ray_task_execution_for_native_flush()
    _start_collector()
    atexit.register(_shutdown_collector)
    _startup_complete = True

    _configure_local_proxy_endpoint()


def _run_worker_entrypoint(argv: list[str]) -> None:
    if not argv:
        return

    # Set preload vars immediately before exec so the final Python worker
    # process, not this bootstrap helper, starts with the interposer active.
    _prepare_preload_env_for_worker_exec()
    os.execvp("python3", ["python3", *argv])


def main() -> None:
    """Entry point called by Ray as py_executable."""
    _startup()
    _run_worker_entrypoint(sys.argv[1:])


if __name__ == "__main__":
    main()
