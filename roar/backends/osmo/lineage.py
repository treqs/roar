from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from roar.execution.fragments.lineage import FragmentLineageBackend, merge_execution_fragments
from roar.execution.fragments.models import ExecutionFragment, derive_fragment_identity


@dataclass(frozen=True)
class OsmoLineageBundle:
    path: str
    dataset_name: str | None = None
    declared_path: str | None = None
    task_name: str | None = None


@dataclass(frozen=True)
class OsmoLineageReconstitutionResult:
    jobs_merged: int = 0
    artifacts_merged: int = 0
    fragments_processed: int = 0
    bundles: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


OSMO_FRAGMENT_LINEAGE_BACKEND = FragmentLineageBackend(
    job_type="osmo_task",
    command_for_fragment=lambda fragment: _osmo_fragment_command(fragment.task_name),
    script_for_fragment=lambda fragment: fragment.task_name or None,
    execution_role_from_fragment=lambda fragment, fallback_parent_job_uid: _osmo_fragment_role(
        fragment,
        fallback_parent_job_uid=fallback_parent_job_uid,
    ),
    metadata_from_fragment=lambda fragment, fallback_parent_job_uid: _osmo_fragment_metadata(
        fragment,
        fallback_parent_job_uid=fallback_parent_job_uid,
    ),
    task_identity_from_metadata=lambda parent_job_uid, job_uid, metadata: derive_fragment_identity(
        "osmo",
        parent_job_uid,
        str(metadata.get("osmo_task_id") or metadata.get("task_id") or ""),
        job_uid,
    ),
)


def reconstitute_osmo_lineage_bundles(
    *,
    bundles: list[OsmoLineageBundle],
    project_dir: str,
    roar_db_path: Path,
    driver_job_uid: str,
    session_id: int | None,
    step_number: int,
) -> OsmoLineageReconstitutionResult:
    normalized_bundles = [bundle for bundle in bundles if str(bundle.path or "").strip()]
    if not normalized_bundles:
        return OsmoLineageReconstitutionResult()

    fragments: list[ExecutionFragment] = []
    bundle_rows: list[dict[str, Any]] = []
    for bundle in normalized_bundles:
        try:
            bundle_fragments = _load_bundle_fragments(Path(bundle.path), project_dir=project_dir)
        except Exception as exc:
            return OsmoLineageReconstitutionResult(
                bundles=_bundle_payloads(normalized_bundles),
                error=f"failed to load lineage bundle {bundle.path}: {exc}",
            )
        fragments.extend(bundle_fragments)
        bundle_rows.append(
            {
                "path": bundle.path,
                "dataset_name": bundle.dataset_name,
                "declared_path": bundle.declared_path,
                "task_name": bundle.task_name,
                "fragments": len(bundle_fragments),
            }
        )

    if not fragments:
        return OsmoLineageReconstitutionResult(bundles=bundle_rows)

    jobs_before, artifacts_before = _count_local_rows(roar_db_path)
    try:
        merge_execution_fragments(
            fragments=fragments,
            project_dir=project_dir,
            backend=OSMO_FRAGMENT_LINEAGE_BACKEND,
            driver_job_uid=driver_job_uid,
            session_id=session_id,
            step_number=step_number,
        )
    except Exception as exc:
        return OsmoLineageReconstitutionResult(
            bundles=bundle_rows,
            fragments_processed=len(fragments),
            error=f"failed to merge lineage bundles: {exc}",
        )

    jobs_after, artifacts_after = _count_local_rows(roar_db_path)
    return OsmoLineageReconstitutionResult(
        jobs_merged=max(0, jobs_after - jobs_before),
        artifacts_merged=max(0, artifacts_after - artifacts_before),
        fragments_processed=len(fragments),
        bundles=bundle_rows,
    )


def discover_downloaded_lineage_bundles(
    datasets: list[dict[str, Any]],
    *,
    bundle_filename: str,
) -> list[OsmoLineageBundle]:
    target_name = str(bundle_filename or "").strip()
    if not target_name:
        return []

    bundles: list[OsmoLineageBundle] = []
    for dataset in datasets:
        local_directory = str(dataset.get("local_directory") or "").strip()
        if not local_directory:
            continue

        declared_path = str(dataset.get("declared_path") or "").strip() or None
        candidate_path: Path | None = None
        if declared_path and Path(declared_path).name == target_name:
            resolved = Path(local_directory) / declared_path
            if resolved.is_file():
                candidate_path = resolved

        if candidate_path is None:
            matches = sorted(Path(local_directory).rglob(target_name))
            if len(matches) == 1:
                candidate_path = matches[0]
            elif len(matches) > 1:
                raise ValueError(
                    f"multiple lineage bundle candidates found in {local_directory} for {target_name}"
                )

        if candidate_path is None or not candidate_path.is_file():
            continue

        bundles.append(
            OsmoLineageBundle(
                path=str(candidate_path),
                dataset_name=str(dataset.get("dataset_name") or "").strip() or None,
                declared_path=declared_path,
                task_name=str(dataset.get("task_name") or "").strip() or None,
            )
        )

    return bundles


def _load_bundle_fragments(bundle_path: Path, *, project_dir: str) -> list[ExecutionFragment]:
    payload = json.loads(bundle_path.read_text(encoding="utf-8"))
    raw_fragments = _extract_raw_fragments(payload)

    fragments: list[ExecutionFragment] = []
    for item in raw_fragments:
        normalized = dict(item)
        normalized.setdefault("backend", "osmo")
        normalized.setdefault("job_uid", "")
        normalized.setdefault("parent_job_uid", "")
        normalized.setdefault("task_id", "")
        normalized.setdefault("worker_id", "")
        normalized.setdefault("node_id", "")
        normalized.setdefault("actor_id", None)
        normalized.setdefault("task_name", "")
        normalized.setdefault("started_at", 0.0)
        normalized.setdefault("ended_at", 0.0)
        normalized.setdefault("exit_code", 0)
        normalized["reads"] = _normalize_ref_payloads(
            normalized.get("reads"),
            project_dir=project_dir,
        )
        normalized["writes"] = _normalize_ref_payloads(
            normalized.get("writes"),
            project_dir=project_dir,
        )
        fragments.append(ExecutionFragment.from_dict(normalized))
    return fragments


def _extract_raw_fragments(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        raise ValueError("lineage bundle payload must be a JSON object or list")

    direct = payload.get("fragments")
    if isinstance(direct, list):
        return [item for item in direct if isinstance(item, dict)]

    execution_fragments = payload.get("execution_fragments")
    if isinstance(execution_fragments, list):
        return [item for item in execution_fragments if isinstance(item, dict)]

    data = payload.get("data")
    if isinstance(data, dict):
        nested = data.get("fragments")
        if isinstance(nested, list):
            return [item for item in nested if isinstance(item, dict)]

    raise ValueError("lineage bundle JSON is missing a fragments list")


def _normalize_ref_payloads(refs: Any, *, project_dir: str) -> list[dict[str, Any]]:
    if not isinstance(refs, list):
        return []

    normalized: list[dict[str, Any]] = []
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        item = dict(ref)
        path = str(item.get("path") or "").strip()
        if path:
            item["path"] = _normalize_fragment_path(path, project_dir=project_dir)
        item.setdefault("hash", None)
        item.setdefault("hash_algorithm", "blake3")
        item["size"] = int(item.get("size") or 0)
        item["capture_method"] = str(item.get("capture_method") or "python")
        normalized.append(item)
    return normalized


def _normalize_fragment_path(path: str, *, project_dir: str) -> str:
    text = str(path or "").strip()
    if not text or "://" in text:
        return text
    if text.startswith("${ROAR_PROJECT_DIR}/"):
        text = text[len("${ROAR_PROJECT_DIR}/") :]
    elif text.startswith("$ROAR_PROJECT_DIR/"):
        text = text[len("$ROAR_PROJECT_DIR/") :]

    candidate = Path(text)
    if candidate.is_absolute():
        return str(candidate)
    return str((Path(project_dir) / candidate).resolve())


def _count_local_rows(roar_db_path: Path) -> tuple[int, int]:
    if not roar_db_path.exists():
        return 0, 0

    conn = sqlite3.connect(str(roar_db_path))
    try:
        jobs = int(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
        artifacts = int(conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0])
        return jobs, artifacts
    finally:
        conn.close()


def _bundle_payloads(bundles: list[OsmoLineageBundle]) -> list[dict[str, Any]]:
    return [
        {
            "path": bundle.path,
            "dataset_name": bundle.dataset_name,
            "declared_path": bundle.declared_path,
            "task_name": bundle.task_name,
        }
        for bundle in bundles
    ]


def _osmo_fragment_command(task_name: str) -> str:
    normalized = str(task_name or "").strip()
    return f"osmo_task:{normalized}" if normalized else "osmo_task"


def _osmo_fragment_role(
    fragment: ExecutionFragment,
    *,
    fallback_parent_job_uid: str | None,
) -> str:
    del fallback_parent_job_uid
    execution_role = str(fragment.backend_metadata.get("execution_role") or "").strip()
    return execution_role or "task"


def _osmo_fragment_metadata(
    fragment: ExecutionFragment,
    *,
    fallback_parent_job_uid: str | None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "osmo_task_id": fragment.task_id,
        "osmo_worker_id": fragment.worker_id,
        "osmo_node_id": fragment.node_id,
    }
    if fragment.actor_id:
        metadata["osmo_actor_id"] = fragment.actor_id
    if fallback_parent_job_uid and not fragment.parent_job_uid:
        metadata["parent_job_uid"] = fallback_parent_job_uid

    backend_metadata = dict(fragment.backend_metadata or {})
    if backend_metadata:
        metadata["backend_metadata"] = backend_metadata
    return metadata


__all__ = [
    "OSMO_FRAGMENT_LINEAGE_BACKEND",
    "OsmoLineageBundle",
    "OsmoLineageReconstitutionResult",
    "discover_downloaded_lineage_bundles",
    "reconstitute_osmo_lineage_bundles",
]
