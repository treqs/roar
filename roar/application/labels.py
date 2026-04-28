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

from ..core.label_origins import LABEL_ORIGIN_USER, build_current_key_origins
from ..db.context import DatabaseContext
from .label_rendering import flatten_label_metadata
from .publish.lineage import LineageCollector
from .publish.remote_job_uids import build_remote_publication_job_uid
from .system_labels import is_reserved_system_label_path, strip_reserved_system_labels


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


class _RemoteLabelMutationDatabaseContext(Protocol):
    @property
    def labels(self) -> Any: ...

    @property
    def sessions(self) -> Any: ...

    @property
    def jobs(self) -> Any: ...

    @property
    def artifacts(self) -> Any: ...


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
            write_origin=LABEL_ORIGIN_USER,
        )
        return LabelWriteResult(
            changed=True,
            metadata=merged,
            version=int(created["version"]),
        )

    def unset_metadata(self, target: LabelTargetRef, keys: tuple[str, ...]) -> LabelWriteResult:
        """Remove user-managed label keys and append a new version if changed."""
        normalized_keys = tuple(_normalize_label_key(key) for key in keys)
        self._reject_reserved_key_paths(normalized_keys)
        current = self.current_metadata(target)
        updated = json.loads(json.dumps(current))
        changed = False
        for key in normalized_keys:
            changed = _remove_nested(updated, key.split(".")) or changed

        if not changed:
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
            updated,
            session_id=target.session_id,
            job_id=target.job_id,
            artifact_id=target.artifact_id,
            write_origin=LABEL_ORIGIN_USER,
        )
        return LabelWriteResult(
            changed=True,
            metadata=updated,
            version=int(created["version"]),
        )

    def copy_metadata(
        self,
        source: LabelTargetRef,
        destination: LabelTargetRef,
    ) -> LabelWriteResult:
        """Copy current source metadata into the destination as a patch."""
        source_metadata = strip_reserved_system_labels(self.current_metadata(source))
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
            write_origin=LABEL_ORIGIN_USER,
        )
        return LabelWriteResult(
            changed=True,
            metadata=merged,
            version=int(created["version"]),
        )

    @staticmethod
    def _reject_reserved_keys(metadata: dict[str, Any]) -> None:
        keys = {key for key, _value in flatten_label_metadata(metadata)}
        LabelService._reject_reserved_key_paths(tuple(keys))

    @staticmethod
    def _reject_reserved_key_paths(keys: tuple[str, ...]) -> None:
        reserved = sorted(key for key in keys if is_reserved_system_label_path(key))
        if reserved:
            joined = ", ".join(reserved)
            raise ValueError(f"Reserved label keys cannot be set manually: {joined}")


def build_remote_label_mutation_payload(
    db_ctx: _RemoteLabelMutationDatabaseContext,
    *,
    roar_dir: Path,
    target: LabelTargetRef,
    metadata: dict[str, Any],
    prefer_remote_publication_uid: bool = True,
) -> dict[str, Any]:
    """Build a GLaaS label-mutation payload for one local target."""
    if target.entity_type == "dag":
        if target.session_id is None:
            raise ValueError("DAG target is missing a local session id.")
        return {
            "entity_type": "dag",
            "session_hash": _canonical_remote_session_hash(
                db_ctx,
                roar_dir=roar_dir,
                session_id=int(target.session_id),
            ),
            "metadata": metadata,
        }

    if target.entity_type == "job":
        if target.job_id is None:
            raise ValueError("Job target is missing a local job id.")
        job = db_ctx.jobs.get(int(target.job_id))
        if not isinstance(job, dict):
            raise ValueError("Job not found.")
        session_id = job.get("session_id")
        job_uid = job.get("job_uid")
        if not isinstance(session_id, int):
            raise ValueError("Job target is missing a local session id.")
        if not isinstance(job_uid, str) or not job_uid:
            raise ValueError("Job target is missing a job UID.")
        session_hash = _canonical_remote_session_hash(
            db_ctx,
            roar_dir=roar_dir,
            session_id=session_id,
        )
        resolved_job_uid = (
            build_remote_publication_job_uid(session_hash, job_uid)
            if prefer_remote_publication_uid
            else job_uid
        )
        return {
            "entity_type": "job",
            "session_hash": session_hash,
            "job_uid": resolved_job_uid,
            "metadata": metadata,
        }

    if target.entity_type == "artifact":
        if target.artifact_id is None:
            raise ValueError("Artifact target is missing a local artifact id.")
        artifact = db_ctx.artifacts.get(str(target.artifact_id))
        if not isinstance(artifact, dict):
            raise ValueError("Artifact not found.")
        artifact_hash = artifact.get("hash")
        if not isinstance(artifact_hash, str) or not artifact_hash:
            hashes = db_ctx.artifacts.get_hashes(str(target.artifact_id))
            artifact_hash = next(
                (
                    item.get("digest")
                    for item in hashes
                    if isinstance(item, dict)
                    and item.get("algorithm") == "blake3"
                    and isinstance(item.get("digest"), str)
                    and item.get("digest")
                ),
                None,
            )
            if not isinstance(artifact_hash, str) or not artifact_hash:
                artifact_hash = next(
                    (
                        item.get("digest")
                        for item in hashes
                        if isinstance(item, dict)
                        and isinstance(item.get("digest"), str)
                        and item.get("digest")
                    ),
                    None,
                )
        if not isinstance(artifact_hash, str) or not artifact_hash:
            raise ValueError("Artifact target is missing a content hash.")
        return {
            "entity_type": "artifact",
            "artifact_hash": artifact_hash,
            "metadata": metadata,
        }

    raise ValueError(f"Unsupported label entity type: {target.entity_type}")


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
            history = db_ctx.labels.get_history("dag", session_id=session_id)
            payloads.append(
                {
                    "entity_type": "dag",
                    "session_hash": session_hash,
                    "metadata": current["metadata"],
                    "key_origins": build_current_key_origins(history),
                }
            )

    seen_jobs: set[tuple[str, str]] = set()
    for job in jobs:
        job_id = job.get("id")
        job_uid = job.get("job_uid")
        remote_job_uid = job.get("remote_job_uid")
        if not isinstance(job_id, int) or not isinstance(job_uid, str) or not job_uid:
            continue
        resolved_remote_job_uid = (
            remote_job_uid if isinstance(remote_job_uid, str) and remote_job_uid else job_uid
        )
        dedupe_key = ("job", resolved_remote_job_uid)
        if dedupe_key in seen_jobs:
            continue
        seen_jobs.add(dedupe_key)
        current = db_ctx.labels.get_current("job", job_id=job_id)
        if current and isinstance(current.get("metadata"), dict):
            history = db_ctx.labels.get_history("job", job_id=job_id)
            payloads.append(
                {
                    "entity_type": "job",
                    "session_hash": session_hash,
                    "job_uid": resolved_remote_job_uid,
                    "metadata": current["metadata"],
                    "key_origins": build_current_key_origins(history),
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
            history = db_ctx.labels.get_history("artifact", artifact_id=artifact_id)
            payloads.append(
                {
                    "entity_type": "artifact",
                    "artifact_hash": artifact_hash,
                    "metadata": current["metadata"],
                    "key_origins": build_current_key_origins(history),
                }
            )

    return payloads


def build_reconcile_payload_for_current_lineage(
    db_ctx: _RemoteLabelMutationDatabaseContext,
    *,
    roar_dir: Path,
) -> tuple[str, list[dict[str, Any]]]:
    """Build a user-managed reconcile payload for the active local lineage."""
    session = db_ctx.sessions.get_active()
    if not isinstance(session, dict) or not isinstance(session.get("id"), int):
        raise ValueError("No active session.")

    session_id = int(session["id"])
    session_hash = _canonical_remote_session_hash(
        db_ctx,
        roar_dir=roar_dir,
        session_id=session_id,
    )
    lineage = LineageCollector().collect_session(session_id, roar_dir)
    payloads = collect_label_sync_payloads(
        db_ctx,
        session_id=session_id,
        session_hash=session_hash,
        jobs=list(getattr(lineage, "jobs", []) or []),
        artifacts=list(getattr(lineage, "artifacts", []) or []),
    )
    return session_hash, _user_managed_reconcile_payloads(payloads)


def build_reconcile_payload_for_target(
    db_ctx: _RemoteLabelMutationDatabaseContext,
    *,
    roar_dir: Path,
    target: LabelTargetRef,
    metadata: dict[str, Any],
) -> tuple[str, list[dict[str, Any]]]:
    """Build a single-target user-managed reconcile payload."""
    payload = build_remote_label_mutation_payload(
        db_ctx,
        roar_dir=roar_dir,
        target=target,
        metadata=metadata,
    )
    session_hash = payload.get("session_hash")
    if not isinstance(session_hash, str) or not session_hash:
        session_hash = _canonical_remote_session_hash_for_target(
            db_ctx,
            roar_dir=roar_dir,
            target=target,
        )
    return session_hash, _user_managed_reconcile_payloads([payload])


def _canonical_remote_session_hash(
    db_ctx: _RemoteLabelMutationDatabaseContext,
    *,
    roar_dir: Path,
    session_id: int,
) -> str:
    from ..core.logging import get_logger
    from .publish.runtime import build_publish_runtime
    from .publish.service import prepare_register_preview_execution

    session = db_ctx.sessions.get(session_id)
    if not isinstance(session, dict):
        raise ValueError("Session not found.")

    lineage = LineageCollector().collect_session(session_id, roar_dir)
    if not getattr(lineage, "jobs", None):
        raise ValueError(
            "Selected session has no tracked steps. Run 'roar run' or 'roar build' first."
        )

    runtime = build_publish_runtime(
        start_dir=str(roar_dir.parent),
        allow_public_without_binding=True,
    )
    prepared = prepare_register_preview_execution(
        runtime=runtime,
        roar_dir=roar_dir,
        cwd=roar_dir.parent,
        session_id=session_id,
        session_hash_override=None,
        logger=get_logger(),
        lineage=lineage,
    )
    return str(prepared.session_hash)


def _canonical_remote_session_hash_for_target(
    db_ctx: _RemoteLabelMutationDatabaseContext,
    *,
    roar_dir: Path,
    target: LabelTargetRef,
) -> str:
    if target.session_id is not None:
        return _canonical_remote_session_hash(
            db_ctx,
            roar_dir=roar_dir,
            session_id=int(target.session_id),
        )

    if target.job_id is not None:
        job = db_ctx.jobs.get(int(target.job_id))
        if isinstance(job, dict) and isinstance(job.get("session_id"), int):
            return _canonical_remote_session_hash(
                db_ctx,
                roar_dir=roar_dir,
                session_id=int(job["session_id"]),
            )
        raise ValueError("Job target is missing a local session id.")

    if target.artifact_id is not None:
        session = db_ctx.sessions.get_active()
        if isinstance(session, dict) and isinstance(session.get("id"), int):
            return _canonical_remote_session_hash(
                db_ctx,
                roar_dir=roar_dir,
                session_id=int(session["id"]),
            )

        jobs = db_ctx.artifacts.get_jobs(str(target.artifact_id))
        for relation in ("produced_by", "consumed_by"):
            for job in jobs.get(relation, []):
                if isinstance(job, dict) and isinstance(job.get("session_id"), int):
                    return _canonical_remote_session_hash(
                        db_ctx,
                        roar_dir=roar_dir,
                        session_id=int(job["session_id"]),
                    )

    raise ValueError("Unable to infer a local session for label sync.")


def _user_managed_reconcile_payloads(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    for payload in payloads:
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            continue
        user_metadata = _prune_empty_containers(strip_reserved_system_labels(metadata))
        if not user_metadata:
            continue
        filtered.append(
            {
                key: value
                for key, value in {
                    **payload,
                    "metadata": user_metadata,
                }.items()
                if key != "key_origins"
            }
        )
    return filtered


def _normalize_label_key(key: str) -> str:
    normalized = str(key or "").strip()
    if not normalized:
        raise ValueError("Label key must not be empty.")
    if "=" in normalized:
        raise ValueError(f"Invalid label key '{key}'. Expected a key path, not key=value.")
    return normalized


def _prune_empty_containers(value: Any) -> Any:
    if isinstance(value, dict):
        pruned = {
            key: _prune_empty_containers(child)
            for key, child in value.items()
        }
        return {key: child for key, child in pruned.items() if child != {}}
    if isinstance(value, list):
        return [_prune_empty_containers(item) for item in value]
    return value


def _remove_nested(root: dict[str, Any], path: list[str]) -> bool:
    if not path:
        return False
    cursor = root
    parents: list[tuple[dict[str, Any], str]] = []
    for key in path[:-1]:
        value = cursor.get(key)
        if not isinstance(value, dict):
            return False
        parents.append((cursor, key))
        cursor = value

    if path[-1] not in cursor:
        return False
    del cursor[path[-1]]

    for parent, key in reversed(parents):
        child = parent.get(key)
        if isinstance(child, dict) and not child:
            del parent[key]
        else:
            break
    return True


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


def _deep_merge(current: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = json.loads(json.dumps(current))
    for key, value in patch.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(existing, value)
        else:
            merged[key] = value
    return merged
