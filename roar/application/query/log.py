"""Application orchestration for the local log query."""

from __future__ import annotations

from ...db.context import create_database_context
from ...presenters.formatting import format_duration, format_timestamp
from .requests import LogQueryRequest


def render_log(request: LogQueryRequest) -> str:
    """Render recent job execution history."""
    with create_database_context(request.roar_dir) as db_ctx:
        session = db_ctx.sessions.get_active()
        if not session:
            return "No active session."

        jobs = db_ctx.jobs.get_by_session(session["id"], limit=20)
        if not jobs:
            return "No log entries found."

    uid_width = 8
    step_width = 5
    timestamp_width = 19
    duration_width = 9
    status_width = 6

    header = (
        f"{'UID':<{uid_width}}  "
        f"{'STEP':<{step_width}}  "
        f"{'TIMESTAMP':<{timestamp_width}}  "
        f"{'DURATION':>{duration_width}}  "
        f"{'STATUS':<{status_width}}  "
        "COMMAND"
    )
    lines = [f"\nJob Log ({len(jobs)} jobs)\n", header, "-" * 72]
    for job in reversed(jobs):
        status = _format_status(job["exit_code"], request.use_color)
        if request.use_color and job["exit_code"] is not None:
            status_display = status
        else:
            status_display = f"{status:<{status_width}}"
        lines.append(
            f"{(job['job_uid'] or '?')[:8]:<{uid_width}}  "
            f"{_format_step(job['step_number'], job['job_type']):<{step_width}}  "
            f"{format_timestamp(job['timestamp']):<{timestamp_width}}  "
            f"{format_duration(job['duration_seconds']):>{duration_width}}  "
            f"{status_display}  "
            f"{job['command'] or ''}"
        )

    return "\n".join(lines)


def _format_step(step_number: int | None, job_type: str | None) -> str:
    if step_number is None:
        return "-"
    prefix = "@B" if job_type == "build" else "@"
    return f"{prefix}{step_number}"


def _format_status(exit_code: int | None, use_color: bool) -> str:
    if exit_code is None:
        return "?"
    if exit_code == 0:
        return "\033[32mOK\033[0m" if use_color else "OK"
    return "\033[31mFAIL\033[0m" if use_color else "FAIL"
