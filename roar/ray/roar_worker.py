from __future__ import annotations

import atexit
import builtins
import collections
import hashlib
import os
import queue
import re
import socket
import struct
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from typing import Any

from roar.ray.fragment import ArtifactRef, TaskFragment, derive_task_uid
from roar.ray.glaas_fragment_streamer import GlaasFragmentStreamer

IOEvent = collections.namedtuple(
    "IOEvent",
    ["kind", "task_id", "path", "hash_value", "hash_algorithm", "size", "capture_method"],
)

_event_queue: queue.Queue[IOEvent | None] = queue.Queue()
_collector_thread: threading.Thread | None = None
_shutdown_event = threading.Event()
_native_events_buffer: list[tuple[str, ArtifactRef]] = []
_native_lock = threading.Lock()

_FLUSH_INTERVAL_SECONDS = float(os.environ.get("ROAR_FRAGMENT_FLUSH_INTERVAL", "2.0"))
_FLUSH_THRESHOLD_EVENTS = int(os.environ.get("ROAR_FRAGMENT_FLUSH_THRESHOLD", "200"))
_PROXY_POLL_INTERVAL_SECONDS = float(os.environ.get("ROAR_PROXY_POLL_INTERVAL", "5.0"))

_PROXY_LOG_RE = re.compile(
    r"^\[S3:(\w+)\]\s+(s3://[^\s]+)"
    r"(?:\s+\((\d+)\s+bytes\))?"
    r"(?:\s+etag=(\S+))?"
)
_S3_WRITE_OPS = frozenset(
    {"PutObject", "UploadPart", "CompleteMultipartUpload", "DeleteObject"}
)

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
_node_agent_handle: Any | None = None
_proxy_configured = False
_proxy_log_index = 0


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
        from roar.config import load_config

        start_dir = os.environ.get("ROAR_PROJECT_DIR") or os.getcwd()
        config = load_config(start_dir=start_dir)
        ray_config = config.get("ray", {}) if isinstance(config, dict) else {}
        if isinstance(ray_config, dict):
            configured = str(ray_config.get("actor_attribution", default)).strip().lower()
            if configured in {"per_call", "per_actor"}:
                return configured
    except Exception:
        pass
    return default


def _get_task_function_name() -> str:
    try:
        ray = sys.modules.get("ray")
        if ray is None:
            return "unknown"
        ctx = ray.get_runtime_context()
        for attr in ("get_task_function_name", "get_task_name"):
            getter = getattr(ctx, attr, None)
            if not callable(getter):
                continue
            name = _to_text(getter())
            if name:
                return name
    except Exception:
        pass
    return "unknown"


def _start_fragment(task_id: str) -> TaskFragment:
    now = time.time()
    roar_job_id = str(os.environ.get("ROAR_JOB_ID", "default"))
    return TaskFragment(
        job_uid=derive_task_uid(roar_job_id, task_id),
        parent_job_uid=str(os.environ.get("ROAR_DRIVER_JOB_UID", "")),
        ray_task_id=task_id,
        ray_worker_id=_get_worker_id() or "",
        ray_node_id=_get_node_id() or "",
        ray_actor_id=_get_actor_id(),
        function_name=_get_task_function_name(),
        started_at=now,
        ended_at=now,
        exit_code=0,
    )


def _append_fragment_ref(fragment: TaskFragment, kind: str, ref: ArtifactRef) -> None:
    if kind == "write":
        fragment.writes.append(ref)
        return
    fragment.reads.append(ref)


def _drain_native_tracer_events() -> list[tuple[str, ArtifactRef]]:
    """Drain buffered native tracer events. Called by collector thread."""
    with _native_lock:
        events = list(_native_events_buffer)
        _native_events_buffer.clear()
    return events


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
    try:
        os.unlink(sock_path)
    except FileNotFoundError:
        pass

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)
    server.listen(8)
    server.settimeout(1.0)

    try:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)
    except Exception:
        pass

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
            except socket.timeout:
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
            except socket.timeout:
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

        kind = "write" if kind_str == "write" else "read"
        ref = ArtifactRef(
            path=path,
            hash=None,
            hash_algorithm="",
            size=0,
            capture_method="native",
        )
        with _native_lock:
            _native_events_buffer.append((kind, ref))


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


def _collect_proxy_logs() -> list[tuple[str, ArtifactRef]]:
    global _proxy_log_index

    if _node_agent_handle is None:
        return []

    try:
        import ray

        result = ray.get(
            _node_agent_handle.get_log_entries_since.remote(_proxy_log_index),
            timeout=5,
        )
        _proxy_log_index = result["current_index"]
        return _parse_proxy_log_lines(result["entries"])
    except Exception:
        return []


def _start_collector() -> None:
    global _collector_thread

    if _collector_thread is not None and _collector_thread.is_alive():
        return

    session_id = os.environ.get("ROAR_SESSION_ID")
    token = os.environ.get("ROAR_FRAGMENT_TOKEN")
    glaas_url = os.environ.get("GLAAS_URL") or os.environ.get("GLAAS_API_URL")

    streamer: GlaasFragmentStreamer | None = None
    if session_id and token and glaas_url:
        try:
            streamer = GlaasFragmentStreamer(
                session_id=session_id,
                token=token,
                glaas_url=glaas_url,
            )
        except Exception as exc:
            _get_logger().warning(
                "Failed to initialize Ray fragment streamer for session %s: %s",
                session_id,
                exc,
            )

    _shutdown_event.clear()

    def _collector_loop() -> None:
        fragment: TaskFragment | None = None
        events_since_flush = 0
        last_flush = time.monotonic()
        last_proxy_poll = time.monotonic()

        def _flush_fragment_batch(*, continuation: bool) -> None:
            nonlocal events_since_flush, fragment, last_flush

            if fragment is None or not (fragment.reads or fragment.writes):
                if continuation and fragment is not None:
                    fragment = _start_fragment(fragment.ray_task_id)
                    last_flush = time.monotonic()
                return

            current_task_id = fragment.ray_task_id
            fragment.ended_at = time.time()
            if streamer is not None:
                try:
                    streamer.append_fragment(fragment.to_dict())
                except Exception as exc:
                    _get_logger().warning("Failed to append Ray fragment: %s", exc)

            fragment = _start_fragment(current_task_id) if continuation else None
            events_since_flush = 0
            last_flush = time.monotonic()

        def _ensure_fragment(task_id: str) -> TaskFragment:
            nonlocal fragment, events_since_flush, last_flush

            normalized_task_id = task_id or ""
            if fragment is None:
                fragment = _start_fragment(normalized_task_id)
                events_since_flush = 0
                last_flush = time.monotonic()
            elif normalized_task_id != fragment.ray_task_id:
                _flush_fragment_batch(continuation=False)
                fragment = _start_fragment(normalized_task_id)
                events_since_flush = 0
                last_flush = time.monotonic()
            return fragment

        def _process_event(event: IOEvent) -> None:
            nonlocal events_since_flush

            current_fragment = _ensure_fragment(event.task_id)
            ref = ArtifactRef(
                path=event.path,
                hash=event.hash_value,
                hash_algorithm=event.hash_algorithm,
                size=event.size,
                capture_method=event.capture_method,
            )
            _append_fragment_ref(current_fragment, event.kind, ref)
            events_since_flush += 1

        def _process_proxy_entries(entries: list[tuple[str, ArtifactRef]]) -> None:
            nonlocal events_since_flush

            if not entries:
                return

            current_fragment = _ensure_fragment(_get_current_task_id())
            for kind, ref in entries:
                _append_fragment_ref(current_fragment, kind, ref)
                events_since_flush += 1

        def _process_native_entries(entries: list[tuple[str, ArtifactRef]]) -> None:
            nonlocal events_since_flush

            if not entries:
                return

            current_fragment = _ensure_fragment(_get_current_task_id())
            for kind, ref in entries:
                _append_fragment_ref(current_fragment, kind, ref)
                events_since_flush += 1

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

            _process_native_entries(_drain_native_tracer_events())

            now = time.monotonic()
            if now - last_proxy_poll >= _PROXY_POLL_INTERVAL_SECONDS:
                _process_proxy_entries(_collect_proxy_logs())
                last_proxy_poll = now

            if events_since_flush > 0 and (
                events_since_flush >= _FLUSH_THRESHOLD_EVENTS
                or now - last_flush >= _FLUSH_INTERVAL_SECONDS
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
        _process_proxy_entries(_collect_proxy_logs())
        _flush_fragment_batch(continuation=False)

        if streamer is not None:
            try:
                streamer.close()
            except Exception as exc:
                _get_logger().warning("Failed to close Ray fragment streamer: %s", exc)

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
) -> None:
    _event_queue.put(
        IOEvent(
            "read",
            _get_current_task_id(),
            path,
            hash_value,
            hash_algorithm,
            size,
            capture_method,
        )
    )


def _log_write(
    *,
    path: str,
    hash_value: str | None,
    hash_algorithm: str,
    size: int,
    capture_method: str,
) -> None:
    _event_queue.put(
        IOEvent(
            "write",
            _get_current_task_id(),
            path,
            hash_value,
            hash_algorithm,
            size,
            capture_method,
        )
    )


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
    global _proxy_configured

    if _startup_complete and not _proxy_configured:
        _proxy_configured = True
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


def _configure_local_proxy_endpoint() -> None:
    """Point S3 traffic at the local node agent proxy for this worker."""
    global _node_agent_handle

    job_id = os.environ.get("ROAR_JOB_ID") or os.environ.get("RAY_JOB_ID")
    if not job_id:
        return

    try:
        import ray
        from roar.ray._agent_names import build_node_agent_name

        ctx = ray.get_runtime_context()
        node_id = ctx.get_node_id()
        if isinstance(node_id, bytes):
            node_id = node_id.hex()
        else:
            to_hex = getattr(node_id, "hex", None)
            if callable(to_hex):
                node_id = _to_text(to_hex())
            else:
                node_id = _to_text(node_id)
        if not node_id:
            return

        agent_name = build_node_agent_name(str(job_id), node_id)
        agent = ray.get_actor(agent_name, namespace="roar")
        _node_agent_handle = agent
        port = ray.get(agent.get_proxy_port.remote(), timeout=10)
        if port:
            original = os.environ.get("AWS_ENDPOINT_URL", "")
            if original:
                os.environ["ROAR_UPSTREAM_S3_ENDPOINT"] = original
            os.environ["AWS_ENDPOINT_URL"] = f"http://127.0.0.1:{port}"
    except Exception as exc:
        _get_logger().warning("Failed to configure local proxy endpoint: %s", exc)


def _configure_proxy_in_background() -> None:
    """Start a daemon thread that configures the proxy endpoint after CoreWorker is ready.

    ray.get_actor() segfaults if called before CoreWorker is initialized (happens
    during worker_process_setup_hook). We wait until global_worker.connected is True
    before attempting the GCS lookup.
    """
    import threading
    import time

    def _deferred_configure():
        global _proxy_configured
        try:
            from ray._private.worker import global_worker
        except Exception:
            return

        # Wait for CoreWorker to be ready (up to 10s).
        for _ in range(100):
            if getattr(global_worker, "connected", False):
                break
            time.sleep(0.1)
        else:
            return

        try:
            _configure_local_proxy_endpoint()
            _proxy_configured = True
        except Exception as exc:
            _get_logger().warning("Deferred proxy config failed: %s", exc)

    t = threading.Thread(target=_deferred_configure, daemon=True)
    t.start()


def _startup() -> None:
    global _startup_complete, _actor_attribution_mode

    if _startup_complete:
        return

    _actor_attribution_mode = _get_actor_attribution()
    if "libroar_tracer_preload" in os.environ.get("LD_PRELOAD", ""):
        _start_native_tracer_socket()
    builtins.open = _tracking_open
    _patch_pandas_parquet()
    _patch_tempfile()
    _start_collector()
    atexit.register(_shutdown_collector)
    _startup_complete = True

    # Configure proxy endpoint in a background thread — ray.get_actor() segfaults
    # if called directly from worker_process_setup_hook (CoreWorker not ready).
    # A short delay lets the worker finish initialization before we query GCS.
    _configure_proxy_in_background()


def _run_worker_entrypoint(argv: list[str]) -> None:
    if not argv:
        return

    os.execvp("python3", ["python3", *argv])


def main() -> None:
    """Entry point called by Ray as py_executable."""
    _startup()
    _run_worker_entrypoint(sys.argv[1:])


if __name__ == "__main__":
    main()
