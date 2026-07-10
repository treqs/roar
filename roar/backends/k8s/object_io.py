"""In-process object-store I/O capture for k8s pods.

Direct S3 access (boto3/botocore, and s3fs/fsspec via aiobotocore) is
HTTP, not file I/O — the syscall tracer never sees it. These hooks are
installed by the sitecustomize import dispatch (the k8s backend's
``RuntimeImportAdapter``) inside every ``ROAR_WRAP``-instrumented Python
process and append one JSON line per successful S3 data operation to
``ROAR_K8S_OBJECT_IO_FILE``. The pod entrypoint folds the events into
the exported execution fragment after the traced command exits.

Best-effort by construction: hooks only record after the real call
succeeds, never raise into user code, and no-op entirely when the
events-file env is unset.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

OBJECT_IO_FILE_ENV = "ROAR_K8S_OBJECT_IO_FILE"

_S3_READ_OPS = frozenset({"GetObject"})
_S3_WRITE_OPS = frozenset({"PutObject", "CompleteMultipartUpload", "CopyObject"})
_PATCH_MARKER = "_roar_k8s_object_io_patched"


def patch_imported_module(module_name: str, module: Any) -> None:
    """Runtime-import dispatch target for the k8s backend."""
    if module_name == "botocore.client":
        _patch_sync_client(module)
    elif module_name == "aiobotocore.client":
        _patch_async_client(module)


def _patch_sync_client(module: Any) -> None:
    base_client = getattr(module, "BaseClient", None)
    if base_client is None or getattr(base_client, _PATCH_MARKER, False):
        return
    original = base_client._make_api_call

    def _make_api_call(self: Any, operation_name: str, api_params: dict[str, Any]) -> Any:
        response = original(self, operation_name, api_params)
        _record_s3_event(self, operation_name, api_params, response)
        return response

    base_client._make_api_call = _make_api_call
    setattr(base_client, _PATCH_MARKER, True)


def _patch_async_client(module: Any) -> None:
    base_client = getattr(module, "AioBaseClient", None)
    if base_client is None or getattr(base_client, _PATCH_MARKER, False):
        return
    original = base_client._make_api_call

    async def _make_api_call(self: Any, operation_name: str, api_params: dict[str, Any]) -> Any:
        response = await original(self, operation_name, api_params)
        _record_s3_event(self, operation_name, api_params, response)
        return response

    base_client._make_api_call = _make_api_call
    setattr(base_client, _PATCH_MARKER, True)


def _record_s3_event(
    client: Any,
    operation_name: str,
    api_params: Any,
    response: Any,
) -> None:
    try:
        events_file = os.environ.get(OBJECT_IO_FILE_ENV, "").strip()
        if not events_file:
            return
        if operation_name not in _S3_READ_OPS and operation_name not in _S3_WRITE_OPS:
            return
        service = getattr(
            getattr(getattr(client, "meta", None), "service_model", None), "service_name", ""
        )
        if service != "s3":
            return
        if not isinstance(api_params, dict):
            return
        bucket = str(api_params.get("Bucket") or "").strip()
        key = str(api_params.get("Key") or "").strip()
        if not bucket or not key:
            return

        mode = "read" if operation_name in _S3_READ_OPS else "write"
        event = {
            "mode": mode,
            "path": f"s3://{bucket}/{key}",
            "operation": operation_name,
            "etag": _normalize_etag(response),
            "size": _event_size(mode, api_params, response),
        }
        with open(events_file, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, separators=(",", ":")) + "\n")
    except Exception:
        return


def _normalize_etag(response: Any) -> str | None:
    if not isinstance(response, dict):
        return None
    etag = response.get("ETag")
    if not isinstance(etag, str):
        return None
    normalized = etag.strip().strip('"').strip()
    return normalized or None


def _event_size(mode: str, api_params: dict[str, Any], response: Any) -> int:
    if mode == "read" and isinstance(response, dict):
        content_length = response.get("ContentLength")
        if isinstance(content_length, int) and content_length >= 0:
            return content_length
        return 0

    body = api_params.get("Body")
    if isinstance(body, (bytes, bytearray)):
        return len(body)
    if isinstance(body, str):
        return len(body.encode("utf-8"))
    try:
        return max(0, int(getattr(body, "seekable", lambda: False)() and _stream_size(body)))
    except Exception:
        return 0


def _stream_size(body: Any) -> int:
    position = body.tell()
    body.seek(0, os.SEEK_END)
    size = body.tell()
    body.seek(position)
    return size


def load_object_io_refs(events_path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read and deduplicate recorded events into fragment artifact refs.

    Last event per (mode, path) wins so re-reads/re-writes carry the most
    recent etag/size.
    """
    if not events_path.is_file():
        return [], []

    winners: dict[tuple[str, str], dict[str, Any]] = {}
    for line in events_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        mode = str(event.get("mode") or "")
        path = str(event.get("path") or "")
        if mode not in ("read", "write") or not path.startswith("s3://"):
            continue
        winners[(mode, path)] = event

    reads: list[dict[str, Any]] = []
    writes: list[dict[str, Any]] = []
    for (mode, path), event in sorted(winners.items()):
        etag = event.get("etag")
        ref = {
            "path": path,
            "hash": etag if isinstance(etag, str) and etag else None,
            "hash_algorithm": "etag" if etag else "",
            "size": int(event.get("size") or 0),
            "capture_method": "python",
        }
        (reads if mode == "read" else writes).append(ref)
    return reads, writes


__all__ = [
    "OBJECT_IO_FILE_ENV",
    "load_object_io_refs",
    "patch_imported_module",
]
