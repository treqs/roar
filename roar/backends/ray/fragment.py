from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from roar.execution.fragments.models import (
    ArtifactRef,
    ExecutionFragment,
    _artifact_ref_from_mapping,
    derive_fragment_identity,
)
from roar.execution.fragments.models import (
    derive_fragment_fallback_identity as derive_execution_fragment_fallback_identity,
)

_TASK_UID_DIGEST_SIZE = 16


@dataclass
class TaskFragment:
    job_uid: str
    parent_job_uid: str
    ray_task_id: str
    ray_worker_id: str
    ray_node_id: str
    ray_actor_id: str | None
    function_name: str
    started_at: float
    ended_at: float
    exit_code: int
    recorded_at: float | None = None
    reads: list[ArtifactRef] = field(default_factory=list)
    writes: list[ArtifactRef] = field(default_factory=list)
    worker_packages: dict[str, str] | None = None
    task_identity: str = ""

    def __post_init__(self) -> None:
        if not self.task_identity:
            self.task_identity = derive_task_identity(
                self.parent_job_uid,
                self.ray_task_id,
                self.job_uid,
            ) or derive_fragment_fallback_identity(self)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["reads"] = [asdict(item) for item in self.reads]
        payload["writes"] = [asdict(item) for item in self.writes]
        return payload

    def to_execution_fragment(self) -> ExecutionFragment:
        return ExecutionFragment(
            job_uid=self.job_uid,
            parent_job_uid=self.parent_job_uid,
            task_id=self.ray_task_id,
            worker_id=self.ray_worker_id,
            node_id=self.ray_node_id,
            actor_id=self.ray_actor_id,
            task_name=self.function_name,
            started_at=self.started_at,
            ended_at=self.ended_at,
            exit_code=self.exit_code,
            backend="ray",
            recorded_at=self.recorded_at,
            reads=list(self.reads),
            writes=list(self.writes),
            worker_packages=self.worker_packages,
            task_identity=self.task_identity,
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> TaskFragment:
        if "ray_task_id" not in payload and "task_id" in payload:
            return cls.from_execution_fragment(ExecutionFragment.from_dict(payload))

        hydrated = dict(payload)
        hydrated.setdefault("parent_job_uid", "")
        hydrated.setdefault("ray_actor_id", None)
        hydrated.setdefault("recorded_at", None)
        hydrated.setdefault("worker_packages", None)
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
        return cls(**hydrated)

    @classmethod
    def from_execution_fragment(cls, fragment: ExecutionFragment) -> TaskFragment:
        return cls(
            job_uid=fragment.job_uid,
            parent_job_uid=fragment.parent_job_uid,
            ray_task_id=fragment.task_id,
            ray_worker_id=fragment.worker_id,
            ray_node_id=fragment.node_id,
            ray_actor_id=fragment.actor_id,
            function_name=fragment.task_name,
            started_at=fragment.started_at,
            ended_at=fragment.ended_at,
            exit_code=fragment.exit_code,
            recorded_at=fragment.recorded_at,
            reads=list(fragment.reads),
            writes=list(fragment.writes),
            worker_packages=fragment.worker_packages,
            task_identity=fragment.task_identity,
        )


def derive_task_uid(job_id: str, ray_task_id: str) -> str:
    """Deterministic collision-resistant hex uid for a Ray task within a roar job."""
    raw = f"{job_id}:{ray_task_id}".encode()
    return hashlib.blake2b(raw, digest_size=_TASK_UID_DIGEST_SIZE).hexdigest()


def derive_task_identity(
    parent_job_uid: str,
    ray_task_id: str,
    job_uid: str = "",
) -> str:
    return derive_fragment_identity("ray", parent_job_uid, ray_task_id, job_uid)


def derive_fragment_fallback_identity(fragment: TaskFragment | Mapping[str, Any]) -> str:
    if isinstance(fragment, TaskFragment):
        execution_fragment = fragment.to_execution_fragment()
    else:
        execution_fragment = TaskFragment.from_dict(dict(fragment)).to_execution_fragment()
    return derive_execution_fragment_fallback_identity(execution_fragment)
