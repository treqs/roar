"""
Shared application-layer label helpers and workflows.

Owns local label parsing, rendering, target resolution, and label-sync payload
construction for the CLI and publish flows.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ..db.context import DatabaseContext
from ..execution.recording.dataset_metadata import AUTO_DATASET_LABEL_KEYS

RESERVED_LABEL_KEYS = set(AUTO_DATASET_LABEL_KEYS)


@dataclass(frozen=True)
class LabelTargetRef:
    """Resolved local label target."""

    entity_type: str
    session_id: int | None = None
    job_id: int | None = None
    artifact_id: str | None = None
    display_target: str | None = None


@dataclass(frozen=True)
class LabelWriteResult:
    """Result of a local label write/copy operation."""

    changed: bool
    metadata: dict[str, Any]
    version: int | None = None


class _LabelSyncDatabaseContext(Protocol):
    @property
    def labels(self) -> Any: ...


def parse_label_pairs(pairs: tuple[str, ...]) -> dict[str, Any]:
    """Parse ``key=value`` pairs into nested metadata."""
    metadata: dict[str, Any] = {}
    for pair in pairs:
        if "=" not in pair:
            raise ValueError(f"Invalid label assignment '{pair}'. Expected key=value.")
        key, raw_value = pair.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"Invalid label assignment '{pair}'. Key must not be empty.")
        _assign_nested(metadata, key.split("."), _parse_scalar(raw_value))
    return metadata


def flatten_label_metadata(metadata: dict[str, Any]) -> list[tuple[str, str]]:
    """Flatten metadata into sorted ``(key, display_value)`` pairs."""
    flat: list[tuple[str, str]] = []

    def _walk(prefix: str, value: Any) -> None:
        if isinstance(value, dict):
            for key in sorted(value.keys()):
                next_prefix = f"{prefix}.{key}" if prefix else key
                _walk(next_prefix, value[key])
            return
        flat.append((prefix, _display_scalar(value)))

    _walk("", metadata)
    return flat


def render_label_lines(metadata: dict[str, Any], indent: str = "") -> list[str]:
    """Render a metadata document as sorted ``key=value`` lines."""
    return [f"{indent}{key}={value}" for key, value in flatten_label_metadata(metadata)]


class LabelService:
    """High-level local label workflow service."""

    def __init__(self, db_ctx: DatabaseContext, cwd: Path):
        self._db = db_ctx
        self._cwd = cwd

    def resolve_target(self, entity_type: str, target: str) -> LabelTargetRef:
        """Resolve a CLI target into a local entity reference."""
        normalized_entity = entity_type.strip().lower()
        normalized_target = target.strip()

        if normalized_entity == "dag":
            if normalized_target == "current":
                session = self._db.sessions.get_active()
            else:
                session = self._db.sessions.get_by_hash_prefix(normalized_target)
            if not session:
                raise ValueError(f"DAG not found: {target}")
            return LabelTargetRef(
                entity_type="dag",
                session_id=int(session["id"]),
                display_target=str(session.get("hash") or target),
            )

        if normalized_entity == "job":
            if normalized_target.startswith("@"):
                session = self._db.sessions.get_active()
                if not session:
                    raise ValueError("No active session.")
                ref = normalized_target[1:]
                job_type = None
                if ref.startswith("B"):
                    job_type = "build"
                    ref = ref[1:]
                try:
                    step_number = int(ref)
                except ValueError as exc:
                    raise ValueError(f"Invalid job reference: {target}") from exc
                job = self._db.sessions.get_step_by_number(
                    int(session["id"]), step_number, job_type=job_type
                )
            else:
                job = self._db.jobs.get_by_uid(normalized_target)
            if not job:
                raise ValueError(f"Job not found: {target}")
            return LabelTargetRef(
                entity_type="job",
                job_id=int(job["id"]),
                display_target=str(job.get("job_uid") or target),
            )

        if normalized_entity == "artifact":
            artifact = None
            expanded = Path(os.path.expanduser(normalized_target))
            if (
                "/" in normalized_target
                or normalized_target.startswith(("./", "../", "~"))
                or expanded.exists()
            ):
                path_obj = expanded if expanded.is_absolute() else self._cwd / expanded
                artifact = self._db.artifacts.get_by_path(os.path.normpath(str(path_obj.resolve())))
            if artifact is None:
                artifact = self._db.artifacts.get_by_hash(normalized_target)
            if artifact is None and not expanded.is_absolute():
                path_obj = self._cwd / expanded
                artifact = self._db.artifacts.get_by_path(os.path.normpath(str(path_obj.resolve())))
            if not artifact:
                raise ValueError(f"Artifact not found: {target}")
            display_target = artifact.get("first_seen_path") or artifact.get("hash") or target
            return LabelTargetRef(
                entity_type="artifact",
                artifact_id=str(artifact["id"]),
                display_target=str(display_target),
            )

        raise ValueError(f"Unsupported label entity type: {entity_type}")

    def current_metadata(self, target: LabelTargetRef) -> dict[str, Any]:
        """Get the current metadata document for a target."""
        current = self._db.labels.get_current(
            target.entity_type,
            session_id=target.session_id,
            job_id=target.job_id,
            artifact_id=target.artifact_id,
        )
        if not current:
            return {}
        metadata = current.get("metadata")
        return metadata if isinstance(metadata, dict) else {}

    def history(self, target: LabelTargetRef) -> list[dict[str, Any]]:
        """Get all versions for a target."""
        return self._db.labels.get_history(
            target.entity_type,
            session_id=target.session_id,
            job_id=target.job_id,
            artifact_id=target.artifact_id,
        )

    def set_metadata(self, target: LabelTargetRef, patch: dict[str, Any]) -> LabelWriteResult:
        """Patch the current metadata document and append a new version if changed."""
        self._reject_reserved_keys(patch)
        current = self.current_metadata(target)
        merged = _deep_merge(current, patch)
        if merged == current:
            current_row = self._db.labels.get_current(
                target.entity_type,
                session_id=target.session_id,
                job_id=target.job_id,
                artifact_id=target.artifact_id,
            )
            current_version = int(current_row["version"]) if current_row else None
            return LabelWriteResult(changed=False, metadata=current, version=current_version)

        created = self._db.labels.create_version(
            target.entity_type,
            merged,
            session_id=target.session_id,
            job_id=target.job_id,
            artifact_id=target.artifact_id,
        )
        return LabelWriteResult(
            changed=True,
            metadata=merged,
            version=int(created["version"]),
        )

    def copy_metadata(
        self,
        source: LabelTargetRef,
        destination: LabelTargetRef,
    ) -> LabelWriteResult:
        """Copy current source metadata into the destination as a patch."""
        source_metadata = _remove_reserved_paths(self.current_metadata(source), RESERVED_LABEL_KEYS)
        destination_metadata = self.current_metadata(destination)
        merged = _deep_merge(destination_metadata, source_metadata)
        if merged == destination_metadata:
            current_row = self._db.labels.get_current(
                destination.entity_type,
                session_id=destination.session_id,
                job_id=destination.job_id,
                artifact_id=destination.artifact_id,
            )
            current_version = int(current_row["version"]) if current_row else None
            return LabelWriteResult(
                changed=False,
                metadata=destination_metadata,
                version=current_version,
            )

        created = self._db.labels.create_version(
            destination.entity_type,
            merged,
            session_id=destination.session_id,
            job_id=destination.job_id,
            artifact_id=destination.artifact_id,
        )
        return LabelWriteResult(
            changed=True,
            metadata=merged,
            version=int(created["version"]),
        )

    @staticmethod
    def _reject_reserved_keys(metadata: dict[str, Any]) -> None:
        keys = {key for key, _value in flatten_label_metadata(metadata)}
        reserved = sorted(keys.intersection(RESERVED_LABEL_KEYS))
        if reserved:
            joined = ", ".join(reserved)
            raise ValueError(f"Reserved label keys cannot be set manually: {joined}")


def collect_label_sync_payloads(
    db_ctx: _LabelSyncDatabaseContext,
    *,
    session_id: int | None,
    session_hash: str,
    jobs: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Collect current local labels for the lineage entities being published."""
    payloads: list[dict[str, Any]] = []

    if session_id is not None:
        current = db_ctx.labels.get_current("dag", session_id=session_id)
        if current and isinstance(current.get("metadata"), dict):
            payloads.append(
                {
                    "entity_type": "dag",
                    "session_hash": session_hash,
                    "metadata": current["metadata"],
                }
            )

    seen_jobs: set[tuple[str, str]] = set()
    for job in jobs:
        job_id = job.get("id")
        job_uid = job.get("job_uid")
        if not isinstance(job_id, int) or not isinstance(job_uid, str) or not job_uid:
            continue
        dedupe_key = ("job", job_uid)
        if dedupe_key in seen_jobs:
            continue
        seen_jobs.add(dedupe_key)
        current = db_ctx.labels.get_current("job", job_id=job_id)
        if current and isinstance(current.get("metadata"), dict):
            payloads.append(
                {
                    "entity_type": "job",
                    "session_hash": session_hash,
                    "job_uid": job_uid,
                    "metadata": current["metadata"],
                }
            )

    seen_artifacts: set[str] = set()
    for artifact in artifacts:
        artifact_id = artifact.get("id")
        artifact_hash = artifact.get("hash")
        if (
            not isinstance(artifact_id, str)
            or not isinstance(artifact_hash, str)
            or not artifact_hash
        ):
            continue
        if artifact_hash in seen_artifacts:
            continue
        seen_artifacts.add(artifact_hash)
        current = db_ctx.labels.get_current("artifact", artifact_id=artifact_id)
        if current and isinstance(current.get("metadata"), dict):
            payloads.append(
                {
                    "entity_type": "artifact",
                    "artifact_hash": artifact_hash,
                    "metadata": current["metadata"],
                }
            )

    return payloads


def _assign_nested(root: dict[str, Any], path: list[str], value: Any) -> None:
    cursor = root
    for key in path[:-1]:
        existing = cursor.get(key)
        if not isinstance(existing, dict):
            existing = {}
            cursor[key] = existing
        cursor = existing
    cursor[path[-1]] = value


def _parse_scalar(raw: str) -> Any:
    stripped = raw.strip()
    lowered = stripped.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    try:
        if lowered.startswith("0") and len(lowered) > 1 and lowered.isdigit():
            raise ValueError
        return int(stripped)
    except ValueError:
        pass
    try:
        if any(ch in lowered for ch in (".", "e")):
            return float(stripped)
    except ValueError:
        pass
    return stripped


def _display_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float, str)):
        return str(value)
    return json.dumps(value, sort_keys=True)


def _deep_merge(current: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = json.loads(json.dumps(current))
    for key, value in patch.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(existing, value)
        else:
            merged[key] = value
    return merged


def _remove_reserved_paths(metadata: dict[str, Any], reserved_paths: set[str]) -> dict[str, Any]:
    cleaned = json.loads(json.dumps(metadata))
    for path in reserved_paths:
        _remove_nested(cleaned, path.split("."))
    return cleaned


def _remove_nested(root: dict[str, Any], path: list[str]) -> None:
    if not path:
        return
    key = path[0]
    if key not in root:
        return
    if len(path) == 1:
        root.pop(key, None)
        return
    child = root.get(key)
    if not isinstance(child, dict):
        return
    _remove_nested(child, path[1:])
    if not child:
        root.pop(key, None)
