"""Application orchestration for the local status query."""

from __future__ import annotations

import time
from pathlib import Path

from ...db.query_context import create_query_database_context
from ...presenters.formatting import format_duration, format_size
from ...publish_auth import load_publish_auth_context, resolve_publish_creator_identity
from ..publish.lineage import LineageCollector
from ..publish.session import compute_canonical_lineage_session_hash
from .git_readiness import collect_git_readiness
from .requests import StatusQueryRequest
from .results import (
    StatusArtifactSummary,
    StatusLatestJobSummary,
    StatusSummary,
)

_NO_ACTIVE_SESSION_MESSAGE = "No active session. Run 'roar run' to create a session first."


class StatusQueryError(RuntimeError):
    """Raised when a status query cannot build a summary."""


def _format_relative_time(timestamp: float | int | None, *, now: float | None = None) -> str:
    """Format a Unix timestamp as a short "N{m,h,d} ago" string.

    Returns "?" for None and "just now" for sub-minute deltas.
    """
    if timestamp is None:
        return "?"
    delta = (now if now is not None else time.time()) - float(timestamp)
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    if delta < 86400 * 30:
        return f"{int(delta // 86400)}d ago"
    return f"{int(delta // (86400 * 30))}mo ago"


def _truncate_command(command: str | None, limit: int = 60) -> str:
    if not command:
        return "?"
    stripped = command.strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[: limit - 1] + "…"


def _step_label(step_number: int | None, job_type: str | None) -> str:
    if step_number is None:
        return "@?"
    prefix = "@B" if job_type == "build" else "@"
    return f"{prefix}{step_number}"


def render_status(request: StatusQueryRequest) -> str:
    """Render a summary of the active session."""
    summary = build_status_summary(request)
    lines: list[str] = []

    if summary.git is not None:
        # Lead with the readiness line so the user can tell at a glance
        # whether the next `roar run` will be refused.
        lines.append(f"Git:       {summary.git.render_line()}")

    step_parts: list[str] = []
    if summary.run_steps:
        step_parts.append(f"{summary.run_steps} run step" + ("s" if summary.run_steps != 1 else ""))
    if summary.build_steps:
        step_parts.append(
            f"{summary.build_steps} build step" + ("s" if summary.build_steps != 1 else "")
        )
    session_pieces = [summary.dag_hash, *step_parts]
    if summary.created_at is not None:
        session_pieces.append(_format_relative_time(summary.created_at))
    lines.append("Session:   " + "  ·  ".join(session_pieces))

    if summary.latest_job is not None:
        job = summary.latest_job
        latest_parts = [
            f"{_step_label(job.step_number, job.job_type)} {_truncate_command(job.command)}",
            format_duration(
                float(job.duration_seconds) if job.duration_seconds is not None else None
            ),
        ]
        if job.exit_code not in (None, 0):
            latest_parts.append(f"exit {job.exit_code}")
        latest_parts.append(_format_relative_time(job.timestamp))
        lines.append("Latest:    " + "  ·  ".join(latest_parts))

    if not summary.artifacts:
        return "\n".join(lines)

    missing_count = sum(1 for artifact in summary.artifacts if not artifact.present)
    header = f"Artifacts ({len(summary.artifacts)})"
    if missing_count:
        header = f"Artifacts ({len(summary.artifacts)}, {missing_count} missing)"

    sized_rows = [(artifact, format_size(artifact.size_bytes)) for artifact in summary.artifacts]
    size_width = max(len(size) for _, size in sized_rows)

    lines.append("")
    lines.append(header + ":")
    for artifact, size in sized_rows:
        hash_prefix = artifact.artifact_hash[:12]
        marker = "" if artifact.present else "  (missing)"
        lines.append(f"  {hash_prefix:<14}{size:>{size_width}}  {artifact.path}{marker}")

    return "\n".join(lines)


def build_status_summary(request: StatusQueryRequest) -> StatusSummary:
    """Build a typed summary of the active session status."""
    with create_query_database_context(request.roar_dir) as db_ctx:
        session = db_ctx.sessions.get_active()
        if not session:
            raise StatusQueryError(_NO_ACTIVE_SESSION_MESSAGE)

        jobs = db_ctx.jobs.get_by_session(session["id"], limit=10000)

        build_steps: set[int] = set()
        run_steps: set[int] = set()
        for job in jobs:
            step = job["step_number"]
            if job["job_type"] == "build":
                build_steps.add(step)
            else:
                run_steps.add(step)

        # `get_by_session` already orders by timestamp DESC, so jobs[0] is
        # the most recent. We display it as the `Latest:` line.
        latest_job_summary: StatusLatestJobSummary | None = None
        if jobs:
            latest = jobs[0]
            latest_job_summary = StatusLatestJobSummary(
                step_number=latest.get("step_number"),
                job_type=latest.get("job_type"),
                command=latest.get("command"),
                duration_seconds=latest.get("duration_seconds"),
                exit_code=latest.get("exit_code"),
                timestamp=latest.get("timestamp"),
            )

        artifacts: list[StatusArtifactSummary] = []
        distinct_outputs = getattr(db_ctx.jobs, "get_distinct_outputs_by_session", None)
        if callable(distinct_outputs):
            outputs = distinct_outputs(session["id"])
        else:
            seen_artifact_ids: set[int | str] = set()
            outputs = []
            for job in jobs:
                for output in db_ctx.jobs.get_outputs(job["id"]):
                    artifact_id = output["artifact_id"]
                    if artifact_id not in seen_artifact_ids:
                        seen_artifact_ids.add(artifact_id)
                        outputs.append(output)

        for output in outputs:
            artifacts.append(
                StatusArtifactSummary(
                    artifact_hash=str(output["artifact_hash"] or ""),
                    size_bytes=int(output["size"] or 0),
                    path=str(output["path"]),
                    present=Path(output["path"]).exists(),
                )
            )

        creator_identity = resolve_publish_creator_identity(
            load_publish_auth_context(request.roar_dir.parent, allow_public_without_binding=True)
        )
        lineage = LineageCollector().collect_session(int(session["id"]), request.roar_dir)
        dag_hash = compute_canonical_lineage_session_hash(
            lineage=lineage,
            creator_identity=creator_identity,
        )

    git_readiness = collect_git_readiness(request.roar_dir.parent)
    created_at = session.get("created_at") if isinstance(session, dict) else None
    return StatusSummary(
        dag_hash=dag_hash,
        build_steps=len(build_steps),
        run_steps=len(run_steps),
        artifacts=artifacts,
        git=git_readiness,
        created_at=created_at,
        latest_job=latest_job_summary,
    )
