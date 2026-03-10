from __future__ import annotations

import os
from collections.abc import Sequence

from roar.ray.collector import collect_fragments
from roar.ray.fragment import ArtifactRef, TaskFragment, derive_task_uid
from roar.ray.glaas_fragment_streamer import GlaasFragmentStreamer
from roar.services.execution.proxy import S3LogEntry

_S3_WRITE_OPS = frozenset({"PutObject", "UploadPart", "CompleteMultipartUpload", "DeleteObject"})


def entry_to_ref(entry: S3LogEntry) -> tuple[str, ArtifactRef] | None:
    operation = str(entry.operation or "").strip()
    if not operation:
        return None

    kind = "write" if operation in _S3_WRITE_OPS else "read"
    path = f"s3://{entry.bucket}/{entry.key}"
    etag = str(entry.etag or "").strip() or None
    return (
        kind,
        ArtifactRef(
            path=path,
            hash=etag,
            hash_algorithm="etag" if etag else "",
            size=int(entry.size_bytes or 0),
            capture_method="proxy",
        ),
    )


def build_proxy_fragment(
    entries: Sequence[S3LogEntry],
    *,
    function_name: str,
    task_id: str,
    parent_job_uid: str | None,
    started_at: float,
    ended_at: float,
    exit_code: int,
    node_id: str = "driver",
    recorded_at: float | None = None,
) -> TaskFragment | None:
    roar_job_id = str(parent_job_uid or os.environ.get("ROAR_JOB_ID", "default"))
    fragment = TaskFragment(
        job_uid=derive_task_uid(roar_job_id, task_id),
        parent_job_uid=roar_job_id,
        ray_task_id=task_id,
        ray_worker_id="",
        ray_node_id=node_id,
        ray_actor_id=None,
        function_name=function_name,
        started_at=started_at,
        ended_at=ended_at,
        exit_code=exit_code,
        recorded_at=recorded_at,
    )

    for entry in entries:
        parsed = entry_to_ref(entry)
        if parsed is None:
            continue
        kind, ref = parsed
        if kind == "write":
            fragment.writes.append(ref)
        else:
            fragment.reads.append(ref)

    if not fragment.reads and not fragment.writes:
        return None
    return fragment


def emit_fragment(fragment: TaskFragment) -> None:
    session_id = str(os.environ.get("ROAR_SESSION_ID", "")).strip()
    token = str(os.environ.get("ROAR_FRAGMENT_TOKEN", "")).strip()
    glaas_url = str(os.environ.get("GLAAS_URL") or "").strip()

    if session_id and token and glaas_url:
        streamer = GlaasFragmentStreamer(
            session_id=session_id,
            token=token,
            glaas_url=glaas_url,
        )
        streamer.append_fragment(fragment.to_dict())
        streamer.close()
        return

    project_dir = str(os.environ.get("ROAR_PROJECT_DIR", "")).strip()
    if not project_dir:
        return

    collect_fragments(
        fragments=[fragment.to_dict()],
        project_dir=project_dir,
        driver_job_uid=os.environ.get("ROAR_JOB_ID"),
    )
