"""Application orchestration for the local status query."""

from __future__ import annotations

from pathlib import Path

from ...db.query_context import create_query_database_context
from ...presenters.formatting import format_size
from .requests import StatusQueryRequest
from .results import StatusArtifactSummary, StatusSummary

_NO_ACTIVE_SESSION_MESSAGE = "No active session. Run 'roar run' to create a session first."


class StatusQueryError(RuntimeError):
    """Raised when a status query cannot build a summary."""


def render_status(request: StatusQueryRequest) -> str:
    """Render a summary of the active session."""
    summary = build_status_summary(request)
    lines = [
        "DAG:",
        f"  Build steps: {summary.build_steps}",
        f"  Run steps:   {summary.run_steps}",
    ]

    if not summary.artifacts:
        return "\n".join(lines)

    present = [artifact for artifact in summary.artifacts if artifact.present]
    missing = [artifact for artifact in summary.artifacts if not artifact.present]
    lines.append(f"\nTracked artifacts ({len(summary.artifacts)} shown):")

    if present:
        lines.append("\nPresent:")
        for artifact in present:
            hash_prefix = artifact.artifact_hash[:12]
            size = format_size(artifact.size_bytes)
            lines.append(f"  {hash_prefix:<20}{size:>6}  {artifact.path}")

    if missing:
        lines.append("\nMissing:")
        for artifact in missing:
            hash_prefix = artifact.artifact_hash[:12]
            size = format_size(artifact.size_bytes)
            lines.append(f"  {hash_prefix:<20}{size:>6}  {artifact.path}")

    lines.append(f"\nTotal: {len(present)} present, {len(missing)} missing")
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

    return StatusSummary(
        build_steps=len(build_steps),
        run_steps=len(run_steps),
        artifacts=artifacts,
    )
