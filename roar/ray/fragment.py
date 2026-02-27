from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ArtifactRef:
    path: str
    hash: str | None
    hash_algorithm: str
    size: int
    capture_method: str


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
    reads: list[ArtifactRef] = field(default_factory=list)
    writes: list[ArtifactRef] = field(default_factory=list)
    worker_packages: dict[str, str] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["reads"] = [asdict(item) for item in self.reads]
        payload["writes"] = [asdict(item) for item in self.writes]
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> TaskFragment:
        hydrated = dict(payload)
        hydrated["reads"] = [ArtifactRef(**item) for item in hydrated.get("reads", [])]
        hydrated["writes"] = [ArtifactRef(**item) for item in hydrated.get("writes", [])]
        return cls(**hydrated)


def derive_task_uid(job_id: str, ray_task_id: str) -> str:
    """Deterministic 8-char hex uid for a Ray task within a roar job."""
    raw = f"{job_id}:{ray_task_id}".encode()
    return hashlib.blake2b(raw, digest_size=4).hexdigest()
