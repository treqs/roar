"""Application orchestration for the local status query."""

from __future__ import annotations

from pathlib import Path

from ...core.bootstrap import bootstrap
from ...db.context import create_database_context
from ...presenters.formatting import format_size
from .requests import StatusQueryRequest


def render_status(request: StatusQueryRequest) -> str:
    """Render a summary of the active session."""
    bootstrap(request.roar_dir)

    with create_database_context(request.roar_dir) as db_ctx:
        session = db_ctx.sessions.get_active()
        if not session:
            return "No active session."

        jobs = db_ctx.jobs.get_by_session(session["id"], limit=10000)

        build_steps: set[int] = set()
        run_steps: set[int] = set()
        for job in jobs:
            step = job["step_number"]
            if job["job_type"] == "build":
                build_steps.add(step)
            else:
                run_steps.add(step)

        lines = [
            "DAG:",
            f"  Build steps: {len(build_steps)}",
            f"  Run steps:   {len(run_steps)}",
        ]

        seen_artifact_ids: set[int] = set()
        artifacts: list[dict] = []
        for job in jobs:
            for output in db_ctx.jobs.get_outputs(job["id"]):
                artifact_id = output["artifact_id"]
                if artifact_id not in seen_artifact_ids:
                    seen_artifact_ids.add(artifact_id)
                    artifacts.append(output)

    if not artifacts:
        return "\n".join(lines)

    present = []
    missing = []
    for artifact in artifacts:
        if Path(artifact["path"]).exists():
            present.append(artifact)
        else:
            missing.append(artifact)

    total = len(present) + len(missing)
    lines.append(f"\nTracked artifacts ({total} shown):")

    if present:
        lines.append("\nPresent:")
        for artifact in present:
            hash_prefix = (artifact["artifact_hash"] or "")[:12]
            size = format_size(artifact["size"])
            lines.append(f"  {hash_prefix:<20}{size:>6}  {artifact['path']}")

    if missing:
        lines.append("\nMissing:")
        for artifact in missing:
            hash_prefix = (artifact["artifact_hash"] or "")[:12]
            size = format_size(artifact["size"])
            lines.append(f"  {hash_prefix:<20}{size:>6}  {artifact['path']}")

    lines.append(f"\nTotal: {len(present)} present, {len(missing)} missing")
    return "\n".join(lines)
