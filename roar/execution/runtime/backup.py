"""
Backup support for reversible run mode.

Extracted from RunCoordinator to keep backup policy separate from orchestration.
"""

from __future__ import annotations

import shlex
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...core.interfaces.logger import ILogger
    from ...core.interfaces.run import RunContext


class PreviousOutputBackupService:
    """Backup outputs from the previous execution of the same script."""

    def backup_previous_outputs(self, ctx: RunContext, logger: ILogger) -> None:
        from ...integrations.config import config_get

        if not config_get("reversible.enabled"):
            return

        from ...db.context import create_database_context
        from ...db.repositories.job import SQLAlchemyJobRepository

        try:
            with create_database_context(ctx.roar_dir) as db_ctx:
                command_str = shlex.join(ctx.command)
                script = SQLAlchemyJobRepository._extract_script(command_str)
                if not script:
                    return

                jobs = db_ctx.jobs.get_by_script(script, limit=1)
                if not jobs:
                    return

                previous_job = jobs[0]
                outputs = db_ctx.jobs.get_outputs(previous_job["id"])
                if not outputs:
                    return

                backup_count = 0
                for output in outputs:
                    output_path = Path(output["path"])
                    if not output_path.exists():
                        continue

                    try:
                        relative_path = output_path.relative_to(ctx.repo_root)
                    except ValueError:
                        continue

                    backup_path = ctx.roar_dir / "backups" / previous_job["job_uid"] / relative_path
                    backup_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(output_path, backup_path)
                    backup_count += 1

                if backup_count > 0:
                    logger.info(
                        "Backed up %d file(s) from previous run (job %s)",
                        backup_count,
                        previous_job["job_uid"][:8],
                    )
        except Exception as e:
            # Backup is best-effort, don't fail the run.
            logger.warning("Failed to backup previous outputs: %s", e)
