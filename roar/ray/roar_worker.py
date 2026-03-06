from __future__ import annotations

import atexit
import builtins
import hashlib
import os
import sys
import time
from collections.abc import Callable
from typing import Any

from roar.ray.fragment import ArtifactRef, TaskFragment, derive_task_uid
from roar.ray.glaas_fragment_streamer import GlaasFragmentStreamer

_real_open = builtins.open

_Blake3Constructor = Callable[[], Any]

try:
    from blake3 import blake3 as _blake3_import
except Exception:
    _blake3_constructor: _Blake3Constructor | None = None
else:
    _blake3_constructor = _blake3_import

_current_task_id: str | None = None
_current_fragment: TaskFragment | None = None
_fragment_streamer: GlaasFragmentStreamer | None = None
_startup_complete = False
_actor_attribution_mode = "per_call"


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


def _get_task_id() -> str | None:
    try:
        ray = sys.modules.get("ray")
        if ray is None:
            return None
        ctx = ray.get_runtime_context()
        task_id = ctx.get_task_id()
        return _to_text(task_id)
    except Exception:
        return None


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


def _finalise_fragment(fragment: TaskFragment) -> None:
    fragment.ended_at = time.time()
    _emit_fragment(fragment)


def _flush_current_fragment() -> None:
    global _current_fragment, _current_task_id

    fragment = _current_fragment
    if fragment is None:
        return

    _current_fragment = None
    _current_task_id = None
    _finalise_fragment(fragment)


def _check_task_boundary() -> None:
    """Called before each I/O event; rotates fragment if task changed."""
    global _current_fragment, _current_task_id

    task_id = _get_task_id()
    actor_id = _get_actor_id()
    attribution = _actor_attribution_mode

    boundary_id = task_id
    if attribution == "per_actor" and actor_id:
        boundary_id = actor_id

    if boundary_id != _current_task_id:
        if _current_fragment is not None:
            _finalise_fragment(_current_fragment)
        _current_task_id = boundary_id
        if boundary_id:
            _current_fragment = _start_fragment(boundary_id)
        else:
            _current_fragment = None


def _is_write_mode(mode: str) -> bool:
    return any(flag in mode for flag in ("w", "a", "x", "+"))


def _should_track_local_path(path: str) -> bool:
    normalized = os.path.abspath(path)
    return not normalized.startswith(("/proc/", "/sys/", "/dev/"))


def _log_write(
    *,
    path: str,
    hash_value: str | None,
    hash_algorithm: str,
    size: int,
    capture_method: str,
) -> None:
    if _current_fragment is None:
        return

    _current_fragment.writes.append(
        ArtifactRef(
            path=path,
            hash=hash_value,
            hash_algorithm=hash_algorithm,
            size=size,
            capture_method=capture_method,
        )
    )
    _current_fragment.ended_at = time.time()
    _emit_fragment(_current_fragment)


def _log_read(
    *,
    path: str,
    hash_value: str | None,
    hash_algorithm: str,
    size: int,
    capture_method: str,
) -> None:
    if _current_fragment is None:
        return

    _current_fragment.reads.append(
        ArtifactRef(
            path=path,
            hash=hash_value,
            hash_algorithm=hash_algorithm,
            size=size,
            capture_method=capture_method,
        )
    )
    _current_fragment.ended_at = time.time()
    _emit_fragment(_current_fragment)


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
    _check_task_boundary()
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


def _normalize_etag(value: Any) -> str | None:
    text = _to_text(value)
    if not text:
        return None
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = text[1:-1]
    return text or None


def _extract_bucket_key(args, kwargs) -> tuple[str | None, str | None]:
    bucket = kwargs.get("Bucket")
    key = kwargs.get("Key")
    if bucket and key:
        return _to_text(bucket), _to_text(key)
    if len(args) >= 2:
        return _to_text(args[0]), _to_text(args[1])
    return None, None


def _extract_upload_file_params(args, kwargs) -> tuple[str | None, str | None, str | None]:
    filename = kwargs.get("Filename")
    bucket = kwargs.get("Bucket")
    key = kwargs.get("Key")

    if filename is None and len(args) >= 1:
        filename = args[0]
    if bucket is None and len(args) >= 2:
        bucket = args[1]
    if key is None and len(args) >= 3:
        key = args[2]

    return _to_text(filename), _to_text(bucket), _to_text(key)


def _body_size_bytes(body: Any) -> int:
    if body is None:
        return 0
    if isinstance(body, str):
        return len(body.encode("utf-8"))
    if isinstance(body, (bytes, bytearray, memoryview)):
        return len(body)

    seek = getattr(body, "seek", None)
    tell = getattr(body, "tell", None)
    if callable(seek) and callable(tell):
        try:
            seek(0, os.SEEK_END)
            size_value = tell()
            seek(0)
            if isinstance(size_value, int):
                return max(0, size_value)
            return max(0, int(size_value))
        except Exception:
            return 0

    return 0


def _wrap_s3_client(client):
    if getattr(client, "_roar_s3_wrapped", False):
        return client

    real_put_object = getattr(client, "put_object", None)
    if callable(real_put_object):

        def _tracked_put_object(*args, **kwargs):
            _check_task_boundary()
            response = real_put_object(*args, **kwargs)
            bucket, key = _extract_bucket_key(args, kwargs)
            if bucket and key and _current_fragment is not None:
                body = kwargs.get("Body")
                size = _body_size_bytes(body)

                _log_write(
                    path=f"s3://{bucket}/{key}",
                    hash_value=_normalize_etag(
                        response.get("ETag") if isinstance(response, dict) else None
                    ),
                    hash_algorithm="etag",
                    size=size,
                    capture_method="proxy",
                )
            return response

        client.put_object = _tracked_put_object

    real_upload_file = getattr(client, "upload_file", None)
    if callable(real_upload_file):

        def _tracked_upload_file(*args, **kwargs):
            _check_task_boundary()
            response = real_upload_file(*args, **kwargs)
            filename, bucket, key = _extract_upload_file_params(args, kwargs)
            if bucket and key and _current_fragment is not None:
                size = 0
                if filename:
                    try:
                        size = max(0, int(os.path.getsize(filename)))
                    except (OSError, ValueError, TypeError):
                        size = 0

                _log_write(
                    path=f"s3://{bucket}/{key}",
                    hash_value=None,
                    hash_algorithm="etag",
                    size=size,
                    capture_method="proxy",
                )
            return response

        client.upload_file = _tracked_upload_file

    real_get_object = getattr(client, "get_object", None)
    if callable(real_get_object):

        def _tracked_get_object(*args, **kwargs):
            _check_task_boundary()
            response = real_get_object(*args, **kwargs)
            bucket, key = _extract_bucket_key(args, kwargs)
            if bucket and key and _current_fragment is not None:
                size_value = response.get("ContentLength") if isinstance(response, dict) else None
                try:
                    size = int(size_value) if size_value is not None else 0
                except (TypeError, ValueError):
                    size = 0

                _log_read(
                    path=f"s3://{bucket}/{key}",
                    hash_value=_normalize_etag(
                        response.get("ETag") if isinstance(response, dict) else None
                    ),
                    hash_algorithm="etag",
                    size=size,
                    capture_method="proxy",
                )
            return response

        client.get_object = _tracked_get_object

    client._roar_s3_wrapped = True
    return client


def _get_fragment_streamer() -> GlaasFragmentStreamer | None:
    global _fragment_streamer

    if _fragment_streamer is not None:
        return _fragment_streamer

    session_id = os.environ.get("ROAR_SESSION_ID")
    token = os.environ.get("ROAR_FRAGMENT_TOKEN")
    glaas_url = os.environ.get("GLAAS_URL") or os.environ.get("GLAAS_API_URL")
    if not session_id or not token or not glaas_url:
        return None

    try:
        _fragment_streamer = GlaasFragmentStreamer(
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
        _fragment_streamer = None

    return _fragment_streamer


def _emit_fragment(fragment: TaskFragment) -> None:
    streamer = _get_fragment_streamer()
    if streamer is None:
        return

    try:
        streamer.append_fragment(fragment.to_dict())
    except Exception as exc:
        _get_logger().warning("Failed to append Ray fragment: %s", exc)


def _shutdown_streamer() -> None:
    global _fragment_streamer

    streamer = _fragment_streamer
    if streamer is None:
        return

    try:
        streamer.close()
    except Exception as exc:
        _get_logger().warning("Failed to close Ray fragment streamer: %s", exc)
    finally:
        _fragment_streamer = None


def _patch_boto3() -> None:
    try:
        import boto3
    except Exception:
        return

    if getattr(boto3, "_roar_worker_boto3_patched", False):
        return

    real_client = boto3.client

    def _tracking_client(service_name, *args, **kwargs):
        client = real_client(service_name, *args, **kwargs)
        if str(service_name).lower() != "s3":
            return client
        return _wrap_s3_client(client)

    boto3.client = _tracking_client
    boto3._roar_worker_boto3_patched = True


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
            _check_task_boundary()
            if isinstance(path, (str, bytes, os.PathLike)):
                resolved = os.path.abspath(os.fspath(path))
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


def _startup() -> None:
    global _startup_complete, _actor_attribution_mode

    if _startup_complete:
        return

    _actor_attribution_mode = _get_actor_attribution()
    builtins.open = _tracking_open
    _patch_boto3()
    _patch_pandas_parquet()
    atexit.register(_shutdown_streamer)
    atexit.register(_flush_current_fragment)
    _startup_complete = True


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
