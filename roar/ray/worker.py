"""
roar Ray worker setup hook.

Installed via runtime_env.worker_process_setup_hook when ROAR_WRAP=1.
Patches builtins.open to capture per-task file I/O, writing each event
immediately to ROAR_LOG_DIR/<task_id>.jsonl on the shared volume.
"""
from __future__ import annotations

import builtins
import json
import os
import time
from typing import Any

# Captured at module import time so the hook doesn't recursively call itself.
_real_open = builtins.open

_LOG_DIR: str = ""
_SKIP_PREFIXES: tuple[str, ...] = ()


def setup() -> None:
    """
    Called once by Ray when a new worker process starts.

    Sets up the file I/O tracking shim. Writes are non-blocking:
    each open() call appends a JSON line to the shared log dir.
    """
    global _LOG_DIR, _SKIP_PREFIXES

    if getattr(setup, "_roar_worker_ready", False):
        return

    _LOG_DIR = os.environ.get("ROAR_LOG_DIR", "/shared/.roar-logs")
    os.makedirs(_LOG_DIR, exist_ok=True)

    # Paths we must never recurse into (the log dir itself, /proc, /sys ...)
    _SKIP_PREFIXES = (
        _LOG_DIR,
        "/proc/",
        "/sys/",
        "/dev/",
    )

    builtins.open = _tracking_open
    _patch_boto3()
    _patch_pandas()
    _patch_pyarrow_filesystem()
    _patch_ray_data()
    setup._roar_worker_ready = True


def _tracking_open(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
    """Replacement for builtins.open that logs file access with task context."""
    result = _real_open(*args, **kwargs)

    try:
        raw_path = args[0] if args else kwargs.get("file", "")
        if isinstance(raw_path, (str, bytes, os.PathLike)):
            path = os.path.abspath(os.fspath(raw_path))
            mode = args[1] if len(args) > 1 else kwargs.get("mode", "r")

            # Skip our own log files and pseudo-filesystems.
            if not any(path.startswith(prefix) for prefix in _SKIP_PREFIXES):
                _log_access(path, str(mode), capture_method="python")
    except Exception:  # noqa: BLE001
        pass  # Never let tracking errors break user code

    return result


def _runtime_context_ids() -> tuple[str | None, str | None]:
    try:
        import ray  # noqa: PLC0415

        ctx = ray.get_runtime_context()
        task_id = _to_text(ctx.get_task_id())
        node_id = _to_text(ctx.get_node_id())
        return task_id, node_id
    except Exception:  # noqa: BLE001
        return None, None


def _to_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        try:
            return value.hex()
        except Exception:  # noqa: BLE001
            return value.decode("utf-8", errors="ignore")
    text = str(value)
    return text or None


def _normalize_etag(value: Any) -> str | None:
    text = _to_text(value)
    if not text:
        return None
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = text[1:-1]
    return text or None


def _log_access(
    path: str,
    mode: str,
    *,
    source_type: str | None = None,
    capture_method: str | None = None,
    operation: str | None = None,
    hash_value: str | None = None,
    byte_range: str | None = None,
) -> None:
    """Append one JSON line to the task-specific log file."""
    task_id, node_id = _runtime_context_ids()
    if not task_id:
        return

    log_file = os.path.join(_LOG_DIR, f"{task_id}.jsonl")
    payload: dict[str, Any] = {
        "path": path,
        "mode": mode,
        "task_id": task_id,
        "ts": time.time(),
    }
    if node_id:
        payload["node_id"] = node_id
    if source_type:
        payload["source_type"] = source_type
    if capture_method:
        payload["capture_method"] = capture_method
    if operation:
        payload["operation"] = operation
    if hash_value:
        payload["hash"] = hash_value
    if byte_range:
        payload["byte_range"] = byte_range

    entry = json.dumps(payload)
    # Use _real_open so we don't recurse through our own hook.
    with _real_open(log_file, "a", encoding="utf-8") as fh:
        fh.write(entry + "\n")


def _patch_boto3() -> None:
    try:
        import boto3  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return

    if getattr(boto3, "_roar_worker_boto3_patched", False):
        return

    real_client = boto3.client

    def _tracking_client(service_name, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        client = real_client(service_name, *args, **kwargs)
        if str(service_name).lower() != "s3":
            return client
        return _wrap_s3_client(client)

    boto3.client = _tracking_client
    boto3._roar_worker_boto3_patched = True


def _wrap_s3_client(client):  # noqa: ANN001
    if getattr(client, "_roar_worker_s3_wrapped", False):
        return client

    real_put_object = getattr(client, "put_object", None)
    if callable(real_put_object):

        def _tracked_put_object(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
            response = real_put_object(*args, **kwargs)
            bucket, key = _extract_bucket_key(args, kwargs)
            if bucket and key:
                _log_access(
                    f"s3://{bucket}/{key}",
                    "w",
                    source_type="s3",
                    capture_method="proxy",
                    operation="PutObject",
                    hash_value=_normalize_etag(
                        response.get("ETag") if isinstance(response, dict) else None
                    ),
                )
            return response

        client.put_object = _tracked_put_object

    real_get_object = getattr(client, "get_object", None)
    if callable(real_get_object):

        def _tracked_get_object(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
            response = real_get_object(*args, **kwargs)
            bucket, key = _extract_bucket_key(args, kwargs)
            if bucket and key:
                _log_access(
                    f"s3://{bucket}/{key}",
                    "r",
                    source_type="s3",
                    capture_method="proxy",
                    operation="GetObject",
                    hash_value=_normalize_etag(
                        response.get("ETag") if isinstance(response, dict) else None
                    ),
                    byte_range=_to_text(kwargs.get("Range")),
                )
            return response

        client.get_object = _tracked_get_object

    client._roar_worker_s3_wrapped = True
    return client


def _extract_bucket_key(args, kwargs) -> tuple[str | None, str | None]:  # noqa: ANN001, ANN002
    bucket = kwargs.get("Bucket")
    key = kwargs.get("Key")
    if bucket and key:
        return _to_text(bucket), _to_text(key)
    if len(args) >= 2:
        return _to_text(args[0]), _to_text(args[1])
    return None, None


def _patch_pandas() -> None:
    try:
        import pandas as pd  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return

    original_to_parquet = getattr(pd.DataFrame, "to_parquet", None)
    if not callable(original_to_parquet):
        return
    if getattr(original_to_parquet, "_roar_worker_patched", False):
        return

    def _tracked_to_parquet(self, path, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        result = original_to_parquet(self, path, *args, **kwargs)
        try:
            if isinstance(path, (str, bytes, os.PathLike)):
                resolved = os.path.abspath(os.fspath(path))
                if not any(resolved.startswith(prefix) for prefix in _SKIP_PREFIXES):
                    # Treat parquet capture as tracer-level for Ray Data/Arrow parity.
                    _log_access(resolved, "w", capture_method="tracer")
        except Exception:  # noqa: BLE001
            pass
        return result

    _tracked_to_parquet._roar_worker_patched = True
    pd.DataFrame.to_parquet = _tracked_to_parquet


def _patch_pyarrow_filesystem() -> None:
    """
    Capture Arrow filesystem reads/writes done by Ray Data worker internals.

    Ray Data relies heavily on pyarrow C++ file IO paths that bypass Python's
    builtins.open. Wrapping filesystem stream open methods closes that gap.
    """
    try:
        import pyarrow.fs as pafs  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return

    if getattr(pafs, "_roar_worker_fs_patched", False):
        return

    wrappers = {
        "open_input_file": "r",
        "open_input_stream": "r",
        "open_output_stream": "w",
        "open_append_stream": "a",
    }

    for method_name, mode in wrappers.items():
        original_method = getattr(pafs.FileSystem, method_name, None)
        if not callable(original_method):
            continue

        def _make_wrapper(original, mode_value):  # noqa: ANN001
            def _wrapped(self, path, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
                result = original(self, path, *args, **kwargs)
                _log_arrow_access(path, mode_value)
                return result

            _wrapped._roar_worker_patched = True
            return _wrapped

        try:
            setattr(pafs.FileSystem, method_name, _make_wrapper(original_method, mode))
        except Exception:  # noqa: BLE001
            continue

    pafs._roar_worker_fs_patched = True


def _patch_ray_data() -> None:
    """
    Fallback capture for Ray Data APIs when Arrow filesystem monkeypatching
    is unavailable in the worker runtime.
    """
    try:
        import ray.data as ray_data  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return

    if getattr(ray_data, "_roar_worker_ray_data_patched", False):
        return

    for method_name, mode in (
        ("read_csv", "r"),
        ("read_parquet", "r"),
        ("read_json", "r"),
        ("read_text", "r"),
    ):
        original_method = getattr(ray_data, method_name, None)
        if not callable(original_method) or getattr(original_method, "_roar_worker_patched", False):
            continue

        def _make_read_wrapper(original, mode_value):  # noqa: ANN001
            def _wrapped(paths, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
                result = original(paths, *args, **kwargs)
                for path in _iter_data_paths(paths):
                    _log_arrow_access(path, mode_value)
                return result

            _wrapped._roar_worker_patched = True
            return _wrapped

        setattr(ray_data, method_name, _make_read_wrapper(original_method, mode))

    try:
        from ray.data.dataset import Dataset  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        ray_data._roar_worker_ray_data_patched = True
        return

    for method_name, mode in (
        ("write_parquet", "w"),
        ("write_csv", "w"),
        ("write_json", "w"),
    ):
        original_method = getattr(Dataset, method_name, None)
        if not callable(original_method) or getattr(original_method, "_roar_worker_patched", False):
            continue

        def _make_write_wrapper(original, mode_value):  # noqa: ANN001
            def _wrapped(self, path, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
                result = original(self, path, *args, **kwargs)
                _log_arrow_access(path, mode_value)
                return result

            _wrapped._roar_worker_patched = True
            return _wrapped

        setattr(Dataset, method_name, _make_write_wrapper(original_method, mode))

    ray_data._roar_worker_ray_data_patched = True


def _iter_data_paths(paths: Any) -> list[str]:
    if isinstance(paths, (str, bytes, os.PathLike)):
        return [os.fspath(paths)]
    if isinstance(paths, (list, tuple, set)):
        return [os.fspath(path) for path in paths if isinstance(path, (str, bytes, os.PathLike))]
    return []


def _log_arrow_access(path: Any, mode: str) -> None:
    if not isinstance(path, (str, bytes, os.PathLike)):
        return
    try:
        resolved = os.path.abspath(os.fspath(path))
    except Exception:  # noqa: BLE001
        return
    if any(resolved.startswith(prefix) for prefix in _SKIP_PREFIXES):
        return
    _log_access(resolved, mode, capture_method="tracer")
