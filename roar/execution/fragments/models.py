from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

_FRAGMENT_IDENTITY_DIGEST_SIZE = 16


@dataclass
class ArtifactRef:
    path: str
    hash: str | None
    hash_algorithm: str
    size: int
    capture_method: str


@dataclass
class ExecutionFragment:
    job_uid: str
    parent_job_uid: str
    task_id: str
    worker_id: str
    node_id: str
    actor_id: str | None
    task_name: str
    started_at: float
    ended_at: float
    exit_code: int
    backend: str
    recorded_at: float | None = None
    reads: list[ArtifactRef] = field(default_factory=list)
    writes: list[ArtifactRef] = field(default_factory=list)
    worker_packages: dict[str, str] | None = None
    task_identity: str = ""
    backend_metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.task_identity:
            self.task_identity = derive_fragment_identity(
                self.backend,
                self.parent_job_uid,
                self.task_id,
                self.job_uid,
            ) or derive_fragment_fallback_identity(self)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["reads"] = [asdict(item) for item in self.reads]
        payload["writes"] = [asdict(item) for item in self.writes]
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ExecutionFragment:
        hydrated = dict(payload)
        hydrated["reads"] = [
            _artifact_ref_from_mapping(item)
            for item in hydrated.get("reads", [])
            if isinstance(item, Mapping)
        ]
        hydrated["writes"] = [
            _artifact_ref_from_mapping(item)
            for item in hydrated.get("writes", [])
            if isinstance(item, Mapping)
        ]
        hydrated["backend_metadata"] = dict(hydrated.get("backend_metadata") or {})
        return cls(**hydrated)


def derive_fragment_identity(
    backend: str,
    parent_job_uid: str,
    task_id: str,
    job_uid: str = "",
) -> str:
    """Deterministic strong identity for fragment/task merge semantics."""
    backend_name = str(backend or "").strip() or "fragment"
    parent = str(parent_job_uid or "").strip()
    normalized_task_id = str(task_id or "").strip()
    short_uid = str(job_uid or "").strip()

    if parent and normalized_task_id:
        raw = f"{backend_name}:{parent}:{normalized_task_id}".encode()
    elif normalized_task_id and short_uid:
        raw = f"{backend_name}-task:{normalized_task_id}:job:{short_uid}".encode()
    elif normalized_task_id:
        raw = f"{backend_name}-task:{normalized_task_id}".encode()
    elif short_uid:
        raw = f"{backend_name}-job:{short_uid}".encode()
    else:
        return ""

    return hashlib.blake2b(raw, digest_size=_FRAGMENT_IDENTITY_DIGEST_SIZE).hexdigest()


def resolve_execution_fragment_identity(
    fragment: ExecutionFragment,
    *,
    fallback_parent_job_uid: str | None = None,
) -> str:
    task_identity = str(fragment.task_identity or "").strip()
    if task_identity:
        return task_identity

    parent_job_uid = str(fragment.parent_job_uid or fallback_parent_job_uid or "").strip()
    return derive_fragment_identity(
        fragment.backend,
        parent_job_uid,
        fragment.task_id,
        fragment.job_uid,
    ) or derive_fragment_fallback_identity(fragment)


def derive_fragment_fallback_identity(fragment: ExecutionFragment | Mapping[str, Any]) -> str:
    payload = _fragment_fallback_payload(fragment)
    if payload is None:
        return ""

    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.blake2b(raw, digest_size=_FRAGMENT_IDENTITY_DIGEST_SIZE).hexdigest()


def _fragment_fallback_payload(
    fragment: ExecutionFragment | Mapping[str, Any],
) -> dict[str, Any] | None:
    backend = str(_fragment_field(fragment, "backend") or "").strip()
    task_name = str(_fragment_field(fragment, "task_name") or "").strip()
    worker_id = str(_fragment_field(fragment, "worker_id") or "").strip()
    node_id = str(_fragment_field(fragment, "node_id") or "").strip()
    actor_id = str(_fragment_field(fragment, "actor_id") or "").strip()
    started_at = _normalize_scalar(_fragment_field(fragment, "started_at"))
    ended_at = _normalize_scalar(_fragment_field(fragment, "ended_at"))
    exit_code = _normalize_scalar(_fragment_field(fragment, "exit_code"))
    recorded_at = _normalize_scalar(_fragment_field(fragment, "recorded_at"))
    reads = _normalize_refs(_fragment_field(fragment, "reads"))
    writes = _normalize_refs(_fragment_field(fragment, "writes"))
    worker_packages = _normalize_worker_packages(_fragment_field(fragment, "worker_packages"))
    backend_metadata = _normalize_backend_metadata(_fragment_field(fragment, "backend_metadata"))

    has_signal = any(
        (
            bool(backend),
            bool(task_name),
            bool(worker_id),
            bool(node_id),
            bool(actor_id),
            started_at is not None,
            ended_at is not None,
            bool(reads),
            bool(writes),
            bool(worker_packages),
            bool(backend_metadata),
        )
    )
    if not has_signal:
        return None

    return {
        "backend": backend,
        "task_name": task_name,
        "worker_id": worker_id,
        "node_id": node_id,
        "actor_id": actor_id,
        "started_at": started_at,
        "ended_at": ended_at,
        "exit_code": exit_code,
        "recorded_at": recorded_at,
        "reads": reads,
        "writes": writes,
        "worker_packages": worker_packages,
        "backend_metadata": backend_metadata,
    }


def _fragment_field(fragment: ExecutionFragment | Mapping[str, Any], name: str) -> Any:
    if isinstance(fragment, ExecutionFragment):
        return getattr(fragment, name)
    return fragment.get(name)


def _normalize_refs(refs: Any) -> list[dict[str, Any]]:
    if not isinstance(refs, list):
        return []

    normalized: list[dict[str, Any]] = []
    for ref in refs:
        item = _normalize_ref(ref)
        if item is not None:
            normalized.append(item)

    return sorted(
        normalized,
        key=lambda item: (
            str(item.get("path") or ""),
            str(item.get("hash") or ""),
            str(item.get("hash_algorithm") or ""),
            int(item.get("size") or 0),
        ),
    )


def _normalize_ref(ref: Any) -> dict[str, Any] | None:
    path: Any
    hash_value: Any
    hash_algorithm: Any
    size: Any
    if isinstance(ref, ArtifactRef):
        path = ref.path
        hash_value = ref.hash
        hash_algorithm = ref.hash_algorithm
        size = ref.size
    elif isinstance(ref, Mapping):
        path = ref.get("path")
        hash_value = ref.get("hash")
        hash_algorithm = ref.get("hash_algorithm")
        size = ref.get("size")
    else:
        return None

    return {
        "path": str(path or ""),
        "hash": str(hash_value or ""),
        "hash_algorithm": str(hash_algorithm or ""),
        "size": _normalize_size(size),
    }


def _normalize_worker_packages(packages: Any) -> list[list[str]]:
    if not isinstance(packages, Mapping):
        return []

    return sorted(
        [
            [str(name or "").strip(), str(version or "").strip()]
            for name, version in packages.items()
        ]
    )


def _normalize_backend_metadata(metadata: Any) -> Any:
    if not isinstance(metadata, Mapping):
        return {}

    return {
        str(key or "").strip(): _normalize_json_like(value)
        for key, value in sorted(metadata.items(), key=lambda item: str(item[0] or ""))
        if str(key or "").strip()
    }


def _normalize_json_like(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return {
            str(key or "").strip(): _normalize_json_like(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0] or ""))
            if str(key or "").strip()
        }
    if isinstance(value, list):
        return [_normalize_json_like(item) for item in value]
    if isinstance(value, tuple):
        return [_normalize_json_like(item) for item in value]
    if isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _normalize_scalar(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _normalize_size(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _artifact_ref_from_mapping(item: Mapping[str, Any]) -> ArtifactRef:
    return ArtifactRef(
        path=str(item.get("path") or ""),
        hash=str(item.get("hash") or "") or None,
        hash_algorithm=str(item.get("hash_algorithm") or ""),
        size=_normalize_size(item.get("size")),
        capture_method=str(item.get("capture_method") or ""),
    )
