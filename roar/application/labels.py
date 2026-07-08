"""
Shared application-layer label helpers and workflows.

Owns local label parsing, rendering, target resolution, and label-sync payload
construction for the CLI and publish flows.
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
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


@dataclass(frozen=True)
class ReconcileTargetSync:
    """Correlates one outbound label-reconcile payload item to the local label
    row(s) it came from and the deletion keys (if any) requested for it.

    A single ``roar label sync`` call can push several targets (dag, jobs,
    artifacts) in one reconcile request. This lets the caller match each
    target's entry in the GLaaS response back to the local rows that should
    (or, if a requested deletion wasn't confirmed, should NOT) get their
    ``synced_at`` baseline advanced. See
    ``roar/application/query/label.py::_mark_labels_synced_confirming_deletions``.
    """

    entity_type: str
    session_hash: str | None
    job_uid: str | None
    artifact_hash: str | None
    label_ids: list[int]
    deleted_keys: list[str]


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

    def delete_keys(self, target: LabelTargetRef, keys: list[str]) -> LabelWriteResult:
        """Remove specific dotted-path keys from the current label document.

        Reserved system-origin keys cannot be deleted (same constraint as for
        `set_metadata`). Empty parent dicts are pruned so deleting `foo.bar`
        from `{"foo": {"bar": "x"}}` leaves `{}` rather than `{"foo": {}}`.
        """
        for key in keys:
            if is_reserved_system_label_path(key):
                raise ValueError(f"Cannot delete reserved label key: {key}")
        current = self.current_metadata(target)
        new_metadata = deepcopy(current)
        for key in keys:
            _remove_nested(new_metadata, key.split("."))
        if new_metadata == current:
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
            new_metadata,
            session_id=target.session_id,
            job_id=target.job_id,
            artifact_id=target.artifact_id,
            write_origin=LABEL_ORIGIN_USER,
        )
        return LabelWriteResult(
            changed=True,
            metadata=new_metadata,
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
    require_published_session: bool = False,
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
                require_published=require_published_session,
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
        session = db_ctx.sessions.get(session_id)
        session_hash = _canonical_remote_session_hash(
            db_ctx,
            roar_dir=roar_dir,
            session_id=session_id,
            require_published=require_published_session,
        )
        published_job_uid = _published_remote_job_uid(session, job_uid)
        if prefer_remote_publication_uid:
            resolved_job_uid = published_job_uid or build_remote_publication_job_uid(
                session_hash,
                job_uid,
            )
        else:
            resolved_job_uid = job_uid
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
            "session_hash": _canonical_remote_session_hash_for_target(
                db_ctx,
                roar_dir=roar_dir,
                target=target,
                require_published=require_published_session,
            ),
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
                    "session_hash": session_hash,
                    "artifact_hash": artifact_hash,
                    "metadata": current["metadata"],
                    "key_origins": build_current_key_origins(history),
                }
            )

    return payloads


def _user_label_key_paths(metadata: Any) -> set[str]:
    if not isinstance(metadata, dict):
        return set()
    return {key for key, _value in flatten_label_metadata(strip_reserved_system_labels(metadata))}


def deleted_user_label_keys(
    db_ctx: _LabelSyncDatabaseContext,
    *,
    entity_type: str,
    session_id: int | None = None,
    job_id: int | None = None,
    artifact_id: str | None = None,
) -> list[str]:
    """User-label key paths removed locally since the last synced version.

    The newest version stamped ``synced_at`` is the remote baseline: keys
    present there but absent from the current version were deliberately unset
    locally and should be deleted remotely on the next reconcile.
    """
    history = db_ctx.labels.get_history(
        entity_type,
        session_id=session_id,
        job_id=job_id,
        artifact_id=artifact_id,
    )
    synced = [row for row in history if row.get("synced_at") is not None]
    if not history or not synced:
        return []
    baseline = max(synced, key=lambda row: int(row.get("version") or 0))
    current = max(history, key=lambda row: int(row.get("version") or 0))
    if int(baseline.get("version") or 0) >= int(current.get("version") or 0):
        return []
    baseline_keys = _user_label_key_paths(baseline.get("metadata"))
    current_keys = _user_label_key_paths(current.get("metadata"))
    return sorted(baseline_keys - current_keys)


def _current_label_row_id(
    db_ctx: _LabelSyncDatabaseContext,
    *,
    entity_type: str,
    session_id: int | None = None,
    job_id: int | None = None,
    artifact_id: str | None = None,
) -> int | None:
    """Local row id of the current label version for a target, if any."""
    current = db_ctx.labels.get_current(
        entity_type,
        session_id=session_id,
        job_id=job_id,
        artifact_id=artifact_id,
    )
    if isinstance(current, dict) and isinstance(current.get("id"), int):
        return int(current["id"])
    return None


def _attach_deleted_user_keys_for_lineage(
    db_ctx: _LabelSyncDatabaseContext,
    payloads: list[dict[str, Any]],
    *,
    session_id: int | None,
    jobs: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
) -> None:
    """Stamp locally-unset user keys and the source label row id onto reconcile
    payloads, per target.

    ``_label_row_id`` is transient bookkeeping consumed by ``_split_label_row_ids``
    below (it is never sent to GLaaS) so the caller can correlate the server's
    response back to the local row(s) that should have ``synced_at`` advanced.
    """
    job_id_by_uid: dict[str, int] = {}
    for job in jobs:
        job_id = job.get("id")
        job_uid = job.get("job_uid")
        remote_job_uid = job.get("remote_job_uid")
        resolved = remote_job_uid if isinstance(remote_job_uid, str) and remote_job_uid else job_uid
        if isinstance(job_id, int) and isinstance(resolved, str) and resolved:
            job_id_by_uid.setdefault(resolved, job_id)

    artifact_id_by_hash: dict[str, str] = {}
    for artifact in artifacts:
        artifact_id = artifact.get("id")
        artifact_hash = artifact.get("hash")
        if isinstance(artifact_id, str) and isinstance(artifact_hash, str) and artifact_hash:
            artifact_id_by_hash.setdefault(artifact_hash, artifact_id)

    for payload in payloads:
        entity_type = payload.get("entity_type")
        deleted: list[str] = []
        label_id: int | None = None
        if entity_type == "dag" and session_id is not None:
            deleted = deleted_user_label_keys(db_ctx, entity_type="dag", session_id=session_id)
            label_id = _current_label_row_id(db_ctx, entity_type="dag", session_id=session_id)
        elif entity_type == "job":
            job_id = job_id_by_uid.get(str(payload.get("job_uid") or ""))
            if job_id is not None:
                deleted = deleted_user_label_keys(db_ctx, entity_type="job", job_id=job_id)
                label_id = _current_label_row_id(db_ctx, entity_type="job", job_id=job_id)
        elif entity_type == "artifact":
            artifact_id = artifact_id_by_hash.get(str(payload.get("artifact_hash") or ""))
            if artifact_id is not None:
                deleted = deleted_user_label_keys(
                    db_ctx,
                    entity_type="artifact",
                    artifact_id=artifact_id,
                )
                label_id = _current_label_row_id(
                    db_ctx, entity_type="artifact", artifact_id=artifact_id
                )
        if deleted:
            payload["deleted_keys"] = deleted
        if label_id is not None:
            payload["_label_row_id"] = label_id


def _split_label_row_ids(
    payloads: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[ReconcileTargetSync]]:
    """Strip the transient ``_label_row_id`` key from outbound wire payloads.

    Returns the cleaned payloads (safe to JSON-serialize to GLaaS) alongside a
    parallel, same-order list of ``ReconcileTargetSync`` records so the caller
    can verify server-confirmed deletions before advancing each target's local
    ``synced_at`` baseline.
    """
    wire_payloads: list[dict[str, Any]] = []
    targets: list[ReconcileTargetSync] = []
    for payload in payloads:
        label_row_id = payload.get("_label_row_id")
        wire_payloads.append({k: v for k, v in payload.items() if k != "_label_row_id"})
        deleted_keys = payload.get("deleted_keys")
        session_hash = payload.get("session_hash")
        job_uid = payload.get("job_uid")
        artifact_hash = payload.get("artifact_hash")
        targets.append(
            ReconcileTargetSync(
                entity_type=str(payload.get("entity_type") or ""),
                session_hash=session_hash if isinstance(session_hash, str) else None,
                job_uid=job_uid if isinstance(job_uid, str) else None,
                artifact_hash=artifact_hash if isinstance(artifact_hash, str) else None,
                label_ids=[int(label_row_id)] if isinstance(label_row_id, int) else [],
                deleted_keys=list(deleted_keys) if isinstance(deleted_keys, list) else [],
            )
        )
    return wire_payloads, targets


def collect_current_label_ids(
    db_ctx: _LabelSyncDatabaseContext,
    *,
    session_id: int | None,
    jobs: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
) -> list[int]:
    """Row ids of the current labels for the entities being published.

    Parallels ``collect_label_sync_payloads`` so the same labels that get pushed
    to GLaaS are the ones stamped ``synced_at`` afterwards.
    """
    ids: list[int] = []

    def _add(current: Any) -> None:
        if (
            isinstance(current, dict)
            and isinstance(current.get("metadata"), dict)
            and isinstance(current.get("id"), int)
        ):
            ids.append(current["id"])

    if session_id is not None:
        _add(db_ctx.labels.get_current("dag", session_id=session_id))

    seen_jobs: set[int] = set()
    for job in jobs:
        job_id = job.get("id")
        if not isinstance(job_id, int) or job_id in seen_jobs:
            continue
        seen_jobs.add(job_id)
        _add(db_ctx.labels.get_current("job", job_id=job_id))

    seen_artifacts: set[str] = set()
    for artifact in artifacts:
        artifact_id = artifact.get("id")
        if not isinstance(artifact_id, str) or not artifact_id or artifact_id in seen_artifacts:
            continue
        seen_artifacts.add(artifact_id)
        _add(db_ctx.labels.get_current("artifact", artifact_id=artifact_id))

    return ids


def count_user_labels(payloads: list[dict[str, Any]]) -> int:
    """Count user-set labels across label-sync payloads.

    System labels live under the reserved ``roar`` namespace key; every other
    top-level metadata key is a user label (e.g. ``roar label set ... key=val``).
    Sums those across all entities so the CLI can report how many user labels
    were synced during a register.
    """
    total = 0
    for payload in payloads:
        metadata = payload.get("metadata")
        if isinstance(metadata, dict):
            total += sum(1 for key in metadata if key != "roar")
    return total


def build_reconcile_payload_for_current_lineage(
    db_ctx: _RemoteLabelMutationDatabaseContext,
    *,
    roar_dir: Path,
) -> tuple[str, list[dict[str, Any]], list[ReconcileTargetSync]]:
    """Build a user-managed reconcile payload for the active local lineage.

    Returns the published session hash, the reconcile label payloads (including
    per-target ``deleted_keys`` for locally-unset user labels), and a per-target
    ``ReconcileTargetSync`` list (same order as the payloads) the caller uses to
    decide which local label rows may have ``synced_at`` stamped after a push —
    see ``roar/application/query/label.py::_mark_labels_synced_confirming_deletions``.
    """
    session = db_ctx.sessions.get_active()
    if not isinstance(session, dict) or not isinstance(session.get("id"), int):
        raise ValueError("No active session.")

    session_id = int(session["id"])
    session_hash = _canonical_remote_session_hash(
        db_ctx,
        roar_dir=roar_dir,
        session_id=session_id,
        require_published=True,
    )
    lineage = LineageCollector().collect_session(session_id, roar_dir)
    jobs = _apply_published_remote_job_uids(
        list(getattr(lineage, "jobs", []) or []),
        session,
    )
    artifacts = list(getattr(lineage, "artifacts", []) or [])
    payloads = collect_label_sync_payloads(
        db_ctx,
        session_id=session_id,
        session_hash=session_hash,
        jobs=jobs,
        artifacts=artifacts,
    )
    _attach_deleted_user_keys_for_lineage(
        db_ctx,
        payloads,
        session_id=session_id,
        jobs=jobs,
        artifacts=artifacts,
    )
    wire_payloads, sync_targets = _split_label_row_ids(_user_managed_reconcile_payloads(payloads))
    return session_hash, wire_payloads, sync_targets


def build_reconcile_payload_for_target(
    db_ctx: _RemoteLabelMutationDatabaseContext,
    *,
    roar_dir: Path,
    target: LabelTargetRef,
    metadata: dict[str, Any],
) -> tuple[str, list[dict[str, Any]], list[ReconcileTargetSync]]:
    """Build a single-target user-managed reconcile payload.

    See ``build_reconcile_payload_for_current_lineage`` for what the returned
    ``ReconcileTargetSync`` list is used for.
    """
    payload = build_remote_label_mutation_payload(
        db_ctx,
        roar_dir=roar_dir,
        target=target,
        metadata=metadata,
        require_published_session=True,
    )
    deleted = deleted_user_label_keys(
        db_ctx,
        entity_type=target.entity_type,
        session_id=target.session_id,
        job_id=target.job_id,
        artifact_id=target.artifact_id,
    )
    if deleted:
        payload["deleted_keys"] = deleted
    session_hash = payload.get("session_hash")
    if not isinstance(session_hash, str) or not session_hash:
        session_hash = _canonical_remote_session_hash_for_target(
            db_ctx,
            roar_dir=roar_dir,
            target=target,
            require_published=True,
        )
    label_id = _current_label_row_id(
        db_ctx,
        entity_type=target.entity_type,
        session_id=target.session_id,
        job_id=target.job_id,
        artifact_id=target.artifact_id,
    )
    if label_id is not None:
        payload["_label_row_id"] = label_id
    wire_payloads, sync_targets = _split_label_row_ids(_user_managed_reconcile_payloads([payload]))
    return session_hash, wire_payloads, sync_targets


def _canonical_remote_session_hash(
    db_ctx: _RemoteLabelMutationDatabaseContext,
    *,
    roar_dir: Path,
    session_id: int,
    require_published: bool = False,
) -> str:
    from ..core.logging import get_logger
    from .publish.runtime import build_publish_runtime
    from .publish.service import prepare_register_preview_execution

    session = db_ctx.sessions.get(session_id)
    if not isinstance(session, dict):
        raise ValueError("Session not found.")

    published_session_hash = _published_remote_session_hash(session)
    if published_session_hash:
        return published_session_hash
    if require_published:
        raise ValueError(_unpublished_lineage_sync_error(session))

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
    require_published: bool = False,
) -> str:
    if target.session_id is not None:
        return _canonical_remote_session_hash(
            db_ctx,
            roar_dir=roar_dir,
            session_id=int(target.session_id),
            require_published=require_published,
        )

    if target.job_id is not None:
        job = db_ctx.jobs.get(int(target.job_id))
        if isinstance(job, dict) and isinstance(job.get("session_id"), int):
            return _canonical_remote_session_hash(
                db_ctx,
                roar_dir=roar_dir,
                session_id=int(job["session_id"]),
                require_published=require_published,
            )
        raise ValueError("Job target is missing a local session id.")

    if target.artifact_id is not None:
        session = db_ctx.sessions.get_active()
        if isinstance(session, dict) and isinstance(session.get("id"), int):
            return _canonical_remote_session_hash(
                db_ctx,
                roar_dir=roar_dir,
                session_id=int(session["id"]),
                require_published=require_published,
            )

        jobs = db_ctx.artifacts.get_jobs(str(target.artifact_id))
        for relation in ("produced_by", "consumed_by"):
            for job in jobs.get(relation, []):
                if isinstance(job, dict) and isinstance(job.get("session_id"), int):
                    return _canonical_remote_session_hash(
                        db_ctx,
                        roar_dir=roar_dir,
                        session_id=int(job["session_id"]),
                        require_published=require_published,
                    )

    raise ValueError("Unable to infer a local session for label sync.")


def _user_managed_reconcile_payloads(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    for payload in payloads:
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            continue
        user_metadata = _prune_empty_containers(strip_reserved_system_labels(metadata))
        deleted_keys = payload.get("deleted_keys")
        has_deletions = isinstance(deleted_keys, list) and bool(deleted_keys)
        # A target whose user labels were all unset still needs a reconcile item
        # so the deletions propagate remotely.
        if not user_metadata and not has_deletions:
            continue
        filtered.append(
            {
                key: value
                for key, value in {
                    **payload,
                    "metadata": user_metadata if isinstance(user_metadata, dict) else {},
                }.items()
                if key != "key_origins"
            }
        )
    return filtered


def _published_remote_session_hash(session: Any) -> str | None:
    publication = _glaas_publication_metadata(session)
    session_hash = publication.get("session_hash") if publication else None
    if isinstance(session_hash, str) and _looks_like_hash(session_hash):
        return session_hash
    return None


def _unpublished_lineage_sync_error(session: dict[str, Any]) -> str:
    local_hash = session.get("hash")
    if isinstance(local_hash, str) and local_hash.strip():
        return (
            "Selected lineage has not been registered to GLaaS yet. "
            f"Run `roar register {local_hash} --yes` or `roar put <artifact> <destination>` "
            "first, then retry `roar label sync`."
        )
    return (
        "Selected lineage has not been registered to GLaaS yet. "
        "Run `roar register <dag-hash> --yes` or `roar put <artifact> <destination>` "
        "first, then retry `roar label sync`."
    )


def _published_remote_job_uid(session: Any, local_job_uid: str) -> str | None:
    publication = _glaas_publication_metadata(session)
    jobs = publication.get("jobs") if publication else None
    if not isinstance(jobs, dict):
        return None
    remote_job_uid = jobs.get(local_job_uid)
    if isinstance(remote_job_uid, str) and remote_job_uid:
        return remote_job_uid
    return None


def _apply_published_remote_job_uids(
    jobs: list[dict[str, Any]],
    session: Any,
) -> list[dict[str, Any]]:
    publication = _glaas_publication_metadata(session)
    job_mapping = publication.get("jobs") if publication else None
    if not isinstance(job_mapping, dict) or not job_mapping:
        return jobs

    annotated: list[dict[str, Any]] = []
    for job in jobs:
        local_job_uid = job.get("job_uid")
        if isinstance(local_job_uid, str):
            remote_job_uid = job_mapping.get(local_job_uid)
        else:
            remote_job_uid = None
        if isinstance(remote_job_uid, str) and remote_job_uid:
            prepared = dict(job)
            prepared["remote_job_uid"] = remote_job_uid
            annotated.append(prepared)
        else:
            annotated.append(job)
    return annotated


def _glaas_publication_metadata(session: Any) -> dict[str, Any] | None:
    if not isinstance(session, dict):
        return None
    metadata = _parse_session_metadata(session.get("metadata"))
    roar_metadata = metadata.get("roar")
    if not isinstance(roar_metadata, dict):
        return None
    remote_publication = roar_metadata.get("remote_publication")
    if not isinstance(remote_publication, dict):
        return None
    glaas = remote_publication.get("glaas")
    return glaas if isinstance(glaas, dict) else None


def _parse_session_metadata(raw_metadata: Any) -> dict[str, Any]:
    if isinstance(raw_metadata, dict):
        return raw_metadata
    if isinstance(raw_metadata, str) and raw_metadata.strip():
        try:
            parsed = json.loads(raw_metadata)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _looks_like_hash(value: str) -> bool:
    normalized = value.strip().lower()
    return len(normalized) == 64 and all(char in "0123456789abcdef" for char in normalized)


def _normalize_label_key(key: str) -> str:
    normalized = str(key or "").strip()
    if not normalized:
        raise ValueError("Label key must not be empty.")
    if "=" in normalized:
        raise ValueError(f"Invalid label key '{key}'. Expected a key path, not key=value.")
    return normalized


def _prune_empty_containers(value: Any) -> Any:
    if isinstance(value, dict):
        pruned = {key: _prune_empty_containers(child) for key, child in value.items()}
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
