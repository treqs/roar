"""
roar Ray log collector.

Called from the driver process atexit handler (when ROAR_WRAP=1).
In Phase 2, Ray lineage is fragments-only:
FragmentReconstituter fetches encrypted fragment batches from GLaaS and this
module adapts those fragments into the shared fragment lineage merge engine.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import Any

from roar.backends.ray.constants import RAY_STEP_NOISE_COMMANDS
from roar.backends.ray.fragment import TaskFragment, derive_task_identity
from roar.execution.fragments.lineage import (
    FragmentLineageBackend,
    assign_execution_fragment_step_numbers,
    merge_execution_fragments,
)
from roar.execution.fragments.models import ExecutionFragment


def _get_logger():
    from roar.core.logging import get_logger

    return get_logger()


RAY_FRAGMENT_LINEAGE_BACKEND = FragmentLineageBackend(
    job_type="ray_task",
    command_for_fragment=lambda fragment: _ray_fragment_command(fragment.task_name),
    script_for_fragment=lambda fragment: fragment.task_name,
    execution_role_from_fragment=lambda fragment, fallback_parent_job_uid: _ray_fragment_role(
        fragment,
        fallback_parent_job_uid=fallback_parent_job_uid,
    ),
    metadata_from_fragment=lambda fragment, fallback_parent_job_uid: _ray_fragment_metadata(
        fragment,
        fallback_parent_job_uid=fallback_parent_job_uid,
    ),
    task_identity_from_metadata=lambda parent_job_uid, job_uid, metadata: derive_task_identity(
        parent_job_uid,
        str(metadata.get("ray_task_id") or metadata.get("task_id") or ""),
        job_uid,
    ),
)


def _apply_reconstitution_filters(
    fragments: list[TaskFragment],
    *,
    project_dir: str,
) -> list[TaskFragment]:
    try:
        from roar.config import load_config
        from roar.services.execution.provenance.file_filter import (
            FileFilterService,
            _get_editable_install_dirs,
        )
    except Exception:
        return [fragment for fragment in fragments if _should_keep_fragment(fragment)]

    config = load_config(start_dir=project_dir)
    filters_config = config.get("filters", {}) if isinstance(config, dict) else {}
    cleanup_config = config.get("cleanup", {}) if isinstance(config, dict) else {}
    ignore_system_reads = bool(filters_config.get("ignore_system_reads", True))
    ignore_package_reads = bool(filters_config.get("ignore_package_reads", True))
    ignore_torch_cache = bool(filters_config.get("ignore_torch_cache", True))
    ignore_tmp_files = bool(filters_config.get("ignore_tmp_files", True))
    if bool(cleanup_config.get("delete_tmp_writes", False)):
        ignore_tmp_files = False

    filter_service = FileFilterService()
    editable_dirs = _get_editable_install_dirs()
    sys_prefix = sys.prefix
    sys_base_prefix = sys.base_prefix

    filtered: list[TaskFragment] = []
    for fragment in fragments:
        for ref in [*fragment.reads, *fragment.writes]:
            ref.path = _normalize_reconstituted_path(str(ref.path or ""), project_dir=project_dir)
        fragment.reads = [
            ref
            for ref in fragment.reads
            if _should_include_ref(
                ref,
                kind="read",
                filter_service=filter_service,
                ignore_system_reads=ignore_system_reads,
                ignore_package_reads=ignore_package_reads,
                ignore_torch_cache=ignore_torch_cache,
                ignore_tmp_files=ignore_tmp_files,
                sys_prefix=sys_prefix,
                sys_base_prefix=sys_base_prefix,
                editable_dirs=editable_dirs,
            )
        ]
        fragment.writes = [
            ref
            for ref in fragment.writes
            if _should_include_ref(
                ref,
                kind="write",
                filter_service=filter_service,
                ignore_system_reads=ignore_system_reads,
                ignore_package_reads=ignore_package_reads,
                ignore_torch_cache=ignore_torch_cache,
                ignore_tmp_files=ignore_tmp_files,
                sys_prefix=sys_prefix,
                sys_base_prefix=sys_base_prefix,
                editable_dirs=editable_dirs,
            )
        ]
        if _should_keep_fragment(fragment):
            filtered.append(fragment)
    return filtered


def _should_keep_fragment(fragment: TaskFragment) -> bool:
    if fragment.reads or fragment.writes:
        return True
    try:
        return float(fragment.ended_at) > float(fragment.started_at)
    except (TypeError, ValueError):
        return False


def _normalize_reconstituted_path(path: str, *, project_dir: str) -> str:
    if not path or path.startswith("s3://"):
        return path

    normalized = os.path.normpath(path)
    marker = f"{os.sep}runtime_resources{os.sep}working_dir_files{os.sep}"
    if marker not in normalized:
        return path

    packaged_suffix = normalized.split(marker, 1)[1]
    packaged_parts = Path(packaged_suffix).parts
    if len(packaged_parts) < 2 or not packaged_parts[0].startswith("_ray_pkg_"):
        return path

    restored = Path(project_dir).joinpath(*packaged_parts[1:])
    return str(restored.resolve(strict=False))


def _should_include_ref(
    ref,
    *,
    kind: str,
    filter_service,
    ignore_system_reads: bool,
    ignore_package_reads: bool,
    ignore_torch_cache: bool,
    ignore_tmp_files: bool,
    sys_prefix: str,
    sys_base_prefix: str,
    editable_dirs,
) -> bool:
    path = str(ref.path or "")
    if not path or path.startswith("s3://"):
        return bool(path)

    if kind == "read":
        if filter_service._is_roar_internal(path) or filter_service._is_git_metadata(path):
            return False
        if ignore_system_reads and filter_service._is_system_read(path):
            return False
        if ignore_torch_cache and filter_service._is_torch_cache(path):
            return False
        if ignore_package_reads and filter_service._is_package_file(
            path,
            sys_prefix,
            sys_base_prefix,
            editable_dirs=editable_dirs,
        ):
            return False
        return not (ignore_tmp_files and filter_service._is_tmp_path(path))

    if filter_service._is_write_noise(path):
        return False
    if ignore_torch_cache and filter_service._is_torch_cache(path):
        return False
    return not (ignore_tmp_files and filter_service._is_tmp_path(path))


def collect(
    project_dir: str | None = None,
    log_dir: str | None = None,
    proxy_logs: dict[str, dict[str, Any]] | None = None,
    fragments: list[dict] | None = None,
) -> None:
    del log_dir, proxy_logs

    if not fragments:
        return

    if project_dir is None:
        project_dir = os.environ.get("ROAR_PROJECT_DIR", "/app")

    db_path = os.path.join(project_dir, ".roar", "roar.db")
    if not os.path.exists(db_path):
        return

    session_id, base_step = _resolve_active_session_context(db_path)
    collect_fragments(
        fragments=fragments,
        project_dir=project_dir,
        driver_job_uid=os.environ.get("ROAR_JOB_ID"),
        session_id=session_id,
        step_number=base_step,
    )


def collect_fragments(
    fragments: list[dict],
    project_dir: str | None = None,
    driver_job_uid: str | None = None,
    session_id: int | None = None,
    step_number: int = 1,
) -> None:
    """Write Ray task fragments to the local DB as child jobs."""
    if project_dir is None:
        project_dir = os.environ.get("ROAR_PROJECT_DIR", "/app")

    db_path = os.path.join(project_dir, ".roar", "roar.db")
    if not os.path.exists(db_path):
        return

    parsed_fragments: list[TaskFragment] = []
    for payload in fragments:
        if not isinstance(payload, dict):
            continue
        try:
            fragment = TaskFragment.from_dict(payload)
        except Exception:
            continue
        parsed_fragments.append(fragment)

    parsed_fragments = _apply_reconstitution_filters(
        parsed_fragments,
        project_dir=project_dir,
    )
    if not parsed_fragments:
        return

    execution_fragments = [fragment.to_execution_fragment() for fragment in parsed_fragments]
    merge_execution_fragments(
        fragments=execution_fragments,
        project_dir=project_dir,
        backend=RAY_FRAGMENT_LINEAGE_BACKEND,
        driver_job_uid=driver_job_uid,
        session_id=session_id,
        step_number=step_number,
    )


def _assign_step_numbers(
    fragments: list[TaskFragment],
    base_step: int = 1,
) -> dict[str, int]:
    execution_fragments = [fragment.to_execution_fragment() for fragment in fragments]
    return assign_execution_fragment_step_numbers(execution_fragments, base_step=base_step)


def _ray_fragment_command(task_name: str) -> str:
    command_name = _task_command_name(task_name)
    return f"ray_task:{command_name}" if command_name else "ray_task"


def _ray_fragment_metadata(
    fragment: ExecutionFragment,
    *,
    fallback_parent_job_uid: str | None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "ray_task_id": fragment.task_id,
        "ray_worker_id": fragment.worker_id,
        "ray_node_id": fragment.node_id,
    }
    if fragment.actor_id:
        metadata["ray_actor_id"] = fragment.actor_id
    if fallback_parent_job_uid and not fragment.parent_job_uid:
        metadata["parent_job_uid"] = fallback_parent_job_uid
    return metadata


def _ray_fragment_role(
    fragment: ExecutionFragment,
    *,
    fallback_parent_job_uid: str | None,
) -> str:
    command = _ray_fragment_command(fragment.task_name)
    if command in RAY_STEP_NOISE_COMMANDS:
        return "noise"

    parent_job_uid = str(fragment.parent_job_uid or fallback_parent_job_uid or "").strip()
    task_name = str(fragment.task_name or "").strip()
    if parent_job_uid and task_name and "." not in task_name:
        return "phase"

    return "task"


def _task_command_name(function_name: str) -> str:
    text = str(function_name or "").strip()
    if not text:
        return ""

    parts = [part for part in text.split(".") if part and part != "<locals>"]
    if not parts:
        return text
    if len(parts) >= 2 and parts[-2][:1].isupper():
        return ".".join(parts[-2:])
    return parts[-1]


def _resolve_active_session_context(db_path: str) -> tuple[int | None, int]:
    if not db_path or not os.path.exists(db_path):
        return None, 1

    try:
        conn = sqlite3.connect(db_path)
    except sqlite3.Error:
        return None, 1

    conn.row_factory = sqlite3.Row
    try:
        try:
            row = conn.execute(
                """
                SELECT id, current_step
                FROM sessions
                WHERE is_active = 1
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
        except sqlite3.Error:
            return None, 1
        if row is None:
            return None, 1
        current_step = int(row["current_step"] or 1)
        return int(row["id"]), max(1, current_step)
    finally:
        conn.close()


def _create_ray_job(conn: sqlite3.Connection, now: float) -> int:
    roar_job_id = os.environ.get("ROAR_JOB_ID")
    if roar_job_id:
        existing_by_uid = conn.execute(
            "SELECT id FROM jobs WHERE job_uid = ? ORDER BY id DESC LIMIT 1",
            (roar_job_id,),
        ).fetchone()
        if existing_by_uid is not None:
            job_id = int(existing_by_uid["id"])
            conn.execute(
                """
                UPDATE jobs
                SET timestamp = ?, status = ?
                WHERE id = ?
                """,
                (now, "completed", job_id),
            )
            return job_id

    existing = conn.execute(
        "SELECT id FROM jobs WHERE job_type = 'ray' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if existing is not None:
        job_id = int(existing["id"])
        conn.execute(
            """
            UPDATE jobs
            SET timestamp = ?, command = ?, status = ?
            WHERE id = ?
            """,
            (now, "ray", "completed", job_id),
        )
        return job_id

    job_uid = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO jobs
            (job_uid, command, script, timestamp, status, job_type)
        VALUES
            (?, ?, ?, ?, ?, ?)
        """,
        (job_uid, "ray", None, now, "completed", "ray"),
    )
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
