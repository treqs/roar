from __future__ import annotations

import atexit
import builtins
import hashlib
import os
import sys
import time
from typing import Any

from roar.ray.fragment import ArtifactRef, TaskFragment, derive_task_uid

_real_open = builtins.open

try:
    from blake3 import blake3 as _blake3_constructor
except Exception:  # noqa: BLE001
    _blake3_constructor = None

_current_task_id: str | None = None
_current_fragment: TaskFragment | None = None
_collector_actor: Any = None
_startup_complete = False
_actor_attribution_mode = "per_call"


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
        except Exception:  # noqa: BLE001
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
    except Exception:  # noqa: BLE001
        return None


def _get_actor_id() -> str | None:
    try:
        ray = sys.modules.get("ray")
        if ray is None:
            return None
        ctx = ray.get_runtime_context()
        actor_id = ctx.get_actor_id()
        return _to_text(actor_id)
    except Exception:  # noqa: BLE001
        return None


def _get_actor_attribution() -> str:
    default = "per_call"
    try:
        from roar.config import load_config  # noqa: PLC0415

        start_dir = os.environ.get("ROAR_PROJECT_DIR") or os.getcwd()
        config = load_config(start_dir=start_dir)
        ray_config = config.get("ray", {}) if isinstance(config, dict) else {}
        if isinstance(ray_config, dict):
            configured = str(ray_config.get("actor_attribution", default)).strip().lower()
            if configured in {"per_call", "per_actor"}:
                return configured
    except Exception:  # noqa: BLE001
        pass
    return default


def _start_fragment(task_id: str) -> TaskFragment:
    now = time.time()
    roar_job_id = str(os.environ.get("ROAR_JOB_ID", "default"))
    return TaskFragment(
        job_uid=derive_task_uid(roar_job_id, task_id),
        parent_job_uid=str(os.environ.get("ROAR_DRIVER_JOB_UID", "")),
        ray_task_id=task_id,
        ray_worker_id="",
        ray_node_id="",
        ray_actor_id=_get_actor_id(),
        function_name="unknown",
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


class _TrackedWriteFile:
    def __init__(self, handle, path: str, capture_method: str = "python") -> None:  # noqa: ANN001
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

    def write(self, data):  # noqa: ANN001
        result = self._handle.write(data)
        self._update_hasher(data)
        return result

    def writelines(self, lines):  # noqa: ANN001
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
            except Exception:  # noqa: BLE001
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

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        self.close()
        return self._handle.__exit__(exc_type, exc, tb)

    @property
    def closed(self) -> bool:
        return self._closed or bool(getattr(self._handle, "closed", False))

    def __getattr__(self, name: str):
        return getattr(self._handle, name)


def _tracking_open(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
    _check_task_boundary()
    handle = _real_open(*args, **kwargs)

    raw_path = args[0] if args else kwargs.get("file")
    mode = args[1] if len(args) > 1 else kwargs.get("mode", "r")

    if _is_write_mode(str(mode)) and isinstance(raw_path, (str, bytes, os.PathLike)):
        path = os.path.abspath(os.fspath(raw_path))
        return _TrackedWriteFile(handle, path=path, capture_method="python")

    return handle


def _normalize_etag(value: Any) -> str | None:
    text = _to_text(value)
    if not text:
        return None
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = text[1:-1]
    return text or None


def _extract_bucket_key(args, kwargs) -> tuple[str | None, str | None]:  # noqa: ANN001, ANN002
    bucket = kwargs.get("Bucket")
    key = kwargs.get("Key")
    if bucket and key:
        return _to_text(bucket), _to_text(key)
    if len(args) >= 2:
        return _to_text(args[0]), _to_text(args[1])
    return None, None


def _wrap_s3_client(client):  # noqa: ANN001
    if getattr(client, "_roar_s3_wrapped", False):
        return client

    real_put_object = getattr(client, "put_object", None)
    if callable(real_put_object):

        def _tracked_put_object(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
            _check_task_boundary()
            response = real_put_object(*args, **kwargs)
            bucket, key = _extract_bucket_key(args, kwargs)
            if bucket and key and _current_fragment is not None:
                body = kwargs.get("Body")
                size = 0
                if isinstance(body, str):
                    size = len(body.encode("utf-8"))
                elif isinstance(body, (bytes, bytearray, memoryview)):
                    size = len(body)

                _current_fragment.writes.append(
                    ArtifactRef(
                        path=f"s3://{bucket}/{key}",
                        hash=_normalize_etag(response.get("ETag") if isinstance(response, dict) else None),
                        hash_algorithm="etag",
                        size=size,
                        capture_method="proxy",
                    )
                )
            return response

        client.put_object = _tracked_put_object

    client._roar_s3_wrapped = True
    return client


def _get_collector_actor():
    global _collector_actor

    if _collector_actor is not None:
        return _collector_actor

    try:
        import ray  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        _collector_actor = None
        return None

    actor_name = f"roar-log-collector-{os.environ.get('ROAR_JOB_ID', 'default')}"
    try:
        _collector_actor = ray.get_actor(actor_name, namespace="roar")
    except Exception:  # noqa: BLE001
        _collector_actor = None

    return _collector_actor


def _emit_fragment(fragment: TaskFragment) -> None:
    actor = _get_collector_actor()
    if actor is None:
        return

    try:
        append_fragment = getattr(actor, "append_fragment", None)
        remote = getattr(append_fragment, "remote", None) if append_fragment is not None else None
        if callable(remote):
            remote(fragment.to_dict())
    except Exception:  # noqa: BLE001
        pass


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


def _startup() -> None:
    global _startup_complete, _actor_attribution_mode

    if _startup_complete:
        return

    _actor_attribution_mode = _get_actor_attribution()
    builtins.open = _tracking_open
    _patch_boto3()
    atexit.register(_flush_current_fragment)
    _startup_complete = True


def _run_worker_entrypoint(argv: list[str]) -> None:
    if not argv:
        return

    os.execvp("python3", ["python3"] + argv)


def main() -> None:
    """Entry point called by Ray as py_executable."""
    _startup()
    _run_worker_entrypoint(sys.argv[1:])


if __name__ == "__main__":
    main()
