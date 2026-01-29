"""
Native Click implementation of the log command.

Usage: roar log
"""

from __future__ import annotations

import sys

import click

from ...db.context import create_database_context
from ...presenters.formatting import format_duration, format_timestamp
from ..context import RoarContext
from ..decorators import require_init


def _format_step(step_number: int | None, job_type: str | None) -> str:
    """Format step number for display."""
    if step_number is None:
        return "-"
    prefix = "@B" if job_type == "build" else "@"
    return f"{prefix}{step_number}"


def _format_status(exit_code: int | None, use_color: bool) -> str:
    """Format job status with optional color."""
    if exit_code is None:
        return "?"
    if exit_code == 0:
        status = "OK"
        if use_color:
            return f"\033[32m{status}\033[0m"  # Green
        return status
    else:
        status = "FAIL"
        if use_color:
            return f"\033[31m{status}\033[0m"  # Red
        return status


@click.command("log")
@click.pass_obj
@require_init
def log(ctx: RoarContext) -> None:
    """Display recent job execution history.

    Shows the 20 most recent jobs in a table format with their
    UID, step reference, timestamp, duration, status, and command.

    \b
    Examples:

        roar log              # Show recent job history
    """
    with create_database_context(ctx.roar_dir) as db_ctx:
        session = db_ctx.sessions.get_active()
        if not session:
            click.echo("No active session.")
            return

        jobs = db_ctx.jobs.get_by_session(session["id"], limit=20)

        if not jobs:
            click.echo("No log entries found.")
            return

        use_color = sys.stdout.isatty()

        # Print header
        click.echo(f"\nJob Log ({len(jobs)} jobs)\n")

        # Column widths (without ANSI codes)
        uid_w = 8
        step_w = 5
        ts_w = 19
        dur_w = 9
        status_w = 6

        # Header
        header = (
            f"{'UID':<{uid_w}}  "
            f"{'STEP':<{step_w}}  "
            f"{'TIMESTAMP':<{ts_w}}  "
            f"{'DURATION':>{dur_w}}  "
            f"{'STATUS':<{status_w}}  "
            f"COMMAND"
        )
        click.echo(header)
        click.echo("-" * 72)

        # Rows (reversed so most recent is at bottom, closest to prompt)
        for job in reversed(jobs):
            uid = job["job_uid"][:8] if job["job_uid"] else "?"
            step = _format_step(job["step_number"], job["job_type"])
            ts = format_timestamp(job["timestamp"])
            dur = format_duration(job["duration_seconds"])
            status = _format_status(job["exit_code"], use_color)
            command = job["command"] or ""

            # Status display width differs when colored
            if use_color and job["exit_code"] is not None:
                # ANSI codes add extra chars, so we don't pad
                status_display = status
            else:
                status_display = f"{status:<{status_w}}"

            row = (
                f"{uid:<{uid_w}}  "
                f"{step:<{step_w}}  "
                f"{ts:<{ts_w}}  "
                f"{dur:>{dur_w}}  "
                f"{status_display}  "
                f"{command}"
            )
            click.echo(row)
