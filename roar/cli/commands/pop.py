"""
Native Click implementation of the pop command.

Usage: roar pop
"""

from __future__ import annotations

import os

import click

from ...db.context import create_database_context
from ...filters.files import FileClassifier
from ..context import RoarContext
from ..decorators import require_init

# Classifications considered safe to delete
_SAFE_CLASSIFICATIONS = {"unmanaged", "repo"}


@click.command("pop", hidden=True)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Skip confirmation prompt.",
)
@click.pass_obj
@require_init
def pop(ctx: RoarContext, yes: bool) -> None:
    """Remove the most recent job from the active session.

    Deletes the job's output artifacts from disk (unless they are
    packages, stdlib, or system files) and removes the job from the
    database.

    \b
    Examples:

        roar pop              # Pop with confirmation prompt

        roar pop -y           # Pop without confirmation
    """
    with create_database_context(ctx.roar_dir) as db_ctx:
        active_session = db_ctx.sessions.get_active()
        if not active_session:
            click.echo("No active session.")
            return

        session_id = active_session["id"]
        steps = db_ctx.sessions.get_steps(session_id)
        if not steps:
            click.echo("No jobs in the active session.")
            return

        # Most recent job: highest step_number / latest timestamp
        latest = max(steps, key=lambda s: (s.get("step_number", 0), s.get("timestamp", 0)))
        job_id = latest["id"]
        step_number = latest.get("step_number")
        command = latest.get("command", "")
        exit_code = latest.get("exit_code")

        click.echo(f"Step {step_number}: {command} (exit {exit_code})")

        if not yes and not click.confirm("Remove this job?", default=True):
            click.echo("Aborted.")
            return

        # Get output artifacts before deleting the job
        outputs = db_ctx.jobs.get_outputs(job_id, db_ctx.artifacts)
        artifact_ids = [o["artifact_id"] for o in outputs]

        # Delete safe output files from disk
        classifier = FileClassifier(repo_root=str(ctx.repo_root or ctx.cwd))
        deleted_files = []
        skipped_files = []
        for output in outputs:
            path = output.get("path") or output.get("first_seen_path")
            if not path:
                continue
            classification, _ = classifier.classify(path)
            if classification in _SAFE_CLASSIFICATIONS:
                if os.path.exists(path):
                    try:
                        os.remove(path)
                        deleted_files.append(path)
                    except OSError:
                        skipped_files.append(path)
                else:
                    deleted_files.append(path)  # already gone
            else:
                skipped_files.append(path)

        # Remove DB records: delete_job removes junction rows and the job itself.
        # Then clean up any artifacts that are now orphaned (not referenced by
        # any remaining job).  We avoid clear_output_records() here because it
        # bulk-deletes JobOutput rows for the given artifact_ids, which would
        # affect other jobs that legitimately reference the same artifacts.
        db_ctx.jobs.delete_job(job_id)
        db_ctx.jobs.cleanup_orphaned_artifacts(artifact_ids, db_ctx.artifacts)

        # Decrement current_step if it pointed to this job
        current_step = active_session.get("current_step", 1)
        if step_number is not None and current_step >= step_number:
            new_step = max(1, step_number - 1)
            db_ctx.sessions.update_current_step(session_id, new_step)

        # Summary
        click.echo(f"Removed job {job_id} (step {step_number}).")
        if deleted_files:
            click.echo(f"Deleted {len(deleted_files)} output file(s).")
        if skipped_files:
            click.echo(f"Skipped {len(skipped_files)} protected file(s).")
