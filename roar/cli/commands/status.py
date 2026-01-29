"""
Native Click implementation of the status command.

Usage: roar status
"""

from __future__ import annotations

from pathlib import Path

import click

from ...core.bootstrap import bootstrap
from ...db.context import create_database_context
from ...presenters.formatting import format_size
from ..context import RoarContext
from ..decorators import require_init


@click.command("status")
@click.pass_obj
@require_init
def status(ctx: RoarContext) -> None:
    """Show a summary of the active session."""
    bootstrap(ctx.roar_dir)

    with create_database_context(ctx.roar_dir) as db_ctx:
        session = db_ctx.sessions.get_active()
        if not session:
            click.echo("No active session.")
            return

        jobs = db_ctx.jobs.get_by_session(session["id"], limit=10000)

        # Count distinct step numbers by job type
        build_steps: set[int] = set()
        run_steps: set[int] = set()
        for job in jobs:
            step = job["step_number"]
            if job["job_type"] == "build":
                build_steps.add(step)
            else:
                run_steps.add(step)

        click.echo("DAG:")
        click.echo(f"  Build steps: {len(build_steps)}")
        click.echo(f"  Run steps:   {len(run_steps)}")

        # Collect unique output artifacts
        seen_artifact_ids: set[int] = set()
        artifacts: list[dict] = []
        for job in jobs:
            for output in db_ctx.jobs.get_outputs(job["id"], db_ctx.artifacts):
                aid = output["artifact_id"]
                if aid not in seen_artifact_ids:
                    seen_artifact_ids.add(aid)
                    artifacts.append(output)

        if not artifacts:
            return

        present = []
        missing = []
        for art in artifacts:
            path = art["path"]
            if Path(path).exists():
                present.append(art)
            else:
                missing.append(art)

        total = len(present) + len(missing)
        click.echo(f"\nTracked artifacts ({total} shown):")

        if present:
            click.echo("\nPresent:")
            for art in present:
                hash_prefix = (art["artifact_hash"] or "")[:12]
                size = format_size(art["size"])
                click.echo(f"  {hash_prefix:<20}{size:>6}  {art['path']}")

        if missing:
            click.echo("\nMissing:")
            for art in missing:
                hash_prefix = (art["artifact_hash"] or "")[:12]
                size = format_size(art["size"])
                click.echo(f"  {hash_prefix:<20}{size:>6}  {art['path']}")

        click.echo(f"\nTotal: {len(present)} present, {len(missing)} missing")
