"""
Run coordinator service - main orchestrator for run/build execution.

Coordinates all services to execute commands with provenance tracking.
Follows SRP: coordinates, doesn't implement details.
"""

import json
import os
import shlex
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from ...core.exceptions import TracerNotFoundError
from ...core.interfaces.logger import ILogger
from ...core.interfaces.presenter import IPresenter
from ...core.interfaces.run import RunContext, RunResult
from .signal_handler import ProcessSignalHandler
from .tracer import TracerService


def _get_logger():
    from ...core.di import resolve_or_default
    from ...core.interfaces.logger import ILogger
    from ...services.logging import NullLogger

    return resolve_or_default(ILogger, NullLogger)


def _collect_telemetry(
    repo_root: str, start_time: float, end_time: float, allow_incomplete: bool = False
):
    """Collect telemetry from registered providers."""
    from ...core.container import get_container

    telemetry_data = {}
    try:
        container = get_container()
        providers = container.get_all_telemetry_providers()
        for name, provider in providers.items():
            if provider.is_available():
                runs = provider.detect_runs(repo_root, start_time, end_time)
                if runs:
                    urls = [run.url for run in runs if run.url]
                    if urls:
                        telemetry_data[name] = urls[0] if len(urls) == 1 else urls
    except Exception as e:
        # Telemetry is best-effort
        _get_logger().debug("Failed to collect telemetry: %s", e)

    return telemetry_data if telemetry_data else None


class RunCoordinator:
    """
    Orchestrates the complete run lifecycle.

    Follows SRP: coordinates, doesn't implement details.
    Follows DIP: depends on service abstractions.
    Follows OCP: new features added via new services.
    """

    def __init__(
        self,
        tracer_service: TracerService | None = None,
        presenter: IPresenter | None = None,
        logger: ILogger | None = None,
    ) -> None:
        """
        Initialize run coordinator.

        Args:
            tracer_service: Service for process tracing
            presenter: Presenter for output
            logger: Logger for internal diagnostics
        """
        self._tracer = tracer_service or TracerService()
        self._presenter = presenter
        self._logger = logger

    @property
    def presenter(self) -> IPresenter:
        """Get presenter, creating default if needed."""
        if self._presenter is None:
            from ...presenters.console import ConsolePresenter

            self._presenter = ConsolePresenter()
        return self._presenter

    @property
    def logger(self) -> ILogger:
        """Get logger, resolving from container or creating NullLogger."""
        if self._logger is None:
            from ...core.container import get_container
            from ...services.logging import NullLogger

            container = get_container()
            self._logger = container.try_resolve(ILogger)  # type: ignore[type-abstract]
            if self._logger is None:
                self._logger = NullLogger()
        return self._logger

    def execute(self, ctx: RunContext) -> RunResult:
        """
        Execute a complete run with all tracking.

        Args:
            ctx: Run context with command and configuration

        Returns:
            RunResult with execution details
        """
        from ...config import load_config
        from .provenance import ProvenanceService

        self.logger.debug(
            "RunCoordinator.execute started: command=%s, job_type=%s", ctx.command, ctx.job_type
        )
        start_time = time.time()
        is_build = ctx.job_type == "build"

        # Create signal handler
        signal_handler = ProcessSignalHandler(
            on_first_interrupt=lambda: self.logger.info(
                "Interrupted. Recording run... (Ctrl-C again to abort)"
            ),
        )

        # Backup previous outputs if reversibility is enabled
        self._backup_previous_outputs(ctx)

        # Execute via tracer
        self.logger.debug("Starting tracer execution")
        try:
            tracer_result = self._tracer.execute(
                ctx.command,
                ctx.roar_dir,
                signal_handler,
            )
            self.logger.debug(
                "Tracer completed: exit_code=%d, duration=%.2fs, interrupted=%s",
                tracer_result.exit_code,
                tracer_result.duration,
                tracer_result.interrupted,
            )
        except TracerNotFoundError as e:
            self.logger.debug("Tracer not found: %s", e)
            self.presenter.print_error(str(e))
            return RunResult(
                exit_code=e.exit_code,
                job_id=0,
                job_uid="000000",
                duration=0,
                inputs=[],
                outputs=[],
                interrupted=False,
                is_build=is_build,
            )

        # Check if we should abort (double Ctrl-C)
        if signal_handler.should_abort():
            self._cleanup_logs(tracer_result.tracer_log_path, tracer_result.inject_log_path)
            sys.exit(130)

        # Load configuration
        config = load_config(start_dir=ctx.repo_root)

        # Check if tracer log exists
        if not os.path.exists(tracer_result.tracer_log_path):
            self.logger.warning("Tracer log not found at %s", tracer_result.tracer_log_path)
            self.logger.warning("The tracer may have failed to start. Run was not recorded.")
            self._cleanup_logs(tracer_result.tracer_log_path, tracer_result.inject_log_path)
            return RunResult(
                exit_code=tracer_result.exit_code,
                job_id=0,
                job_uid="000000",
                duration=tracer_result.duration,
                inputs=[],
                outputs=[],
                interrupted=tracer_result.interrupted,
                is_build=is_build,
            )

        # Collect provenance
        self.logger.debug("Collecting provenance data")
        inject_log = (
            tracer_result.inject_log_path if os.path.exists(tracer_result.inject_log_path) else None
        )
        provenance_service = ProvenanceService()
        prov = provenance_service.collect(
            ctx.repo_root,
            tracer_result.tracer_log_path,
            inject_log,
            config,
        )
        self.logger.debug(
            "Provenance collected: read_files=%d, written_files=%d",
            len(prov.get("data", {}).get("read_files", [])),
            len(prov.get("data", {}).get("written_files", [])),
        )

        # Record in database
        self.logger.debug("Recording job in database")
        job_id, job_uid, read_file_info, written_file_info, stale_upstream, stale_downstream = (
            self._record_job(ctx, prov, tracer_result, start_time, is_build)
        )
        self.logger.debug(
            "Job recorded: id=%d, uid=%s, inputs=%d, outputs=%d",
            job_id,
            job_uid[:12] if job_uid else None,
            len(read_file_info),
            len(written_file_info),
        )

        # Cleanup temp files
        self.logger.debug("Cleaning up temporary log files")
        self._cleanup_logs(tracer_result.tracer_log_path, tracer_result.inject_log_path)

        self.logger.debug(
            "RunCoordinator.execute completed: exit_code=%d, duration=%.2fs",
            tracer_result.exit_code,
            tracer_result.duration,
        )
        return RunResult(
            exit_code=tracer_result.exit_code,
            job_id=job_id,
            job_uid=job_uid,
            duration=tracer_result.duration,
            inputs=read_file_info,
            outputs=written_file_info,
            interrupted=tracer_result.interrupted,
            is_build=is_build,
            stale_upstream=stale_upstream,
            stale_downstream=stale_downstream,
        )

    def _record_job(
        self,
        ctx: RunContext,
        prov: dict[str, Any],
        tracer_result,
        start_time: float,
        is_build: bool,
    ) -> tuple:
        """Record job in database and return file info."""
        from ...db.context import create_database_context

        if is_build:
            written_files = []
            read_files = []
        else:
            written_files = prov.get("data", {}).get("written_files", [])
            read_files = prov.get("data", {}).get("read_files", [])

        git_info = prov.get("executables", {}).get("code", {}).get("git", {})
        git_commit = git_info.get("commit")
        git_branch = git_info.get("branch")
        git_repo = git_info.get("remote_url")

        # Compute working directory relative to repo root
        cwd_relative = None
        try:
            cwd_relative = str(Path.cwd().relative_to(Path(ctx.repo_root)))
            if cwd_relative == ".":
                cwd_relative = ""
        except ValueError:
            pass

        # Build metadata from provenance
        metadata = {}
        if prov.get("executables", {}).get("packages"):
            metadata["packages"] = prov["executables"]["packages"]
        if prov.get("runtime"):
            metadata["runtime"] = prov["runtime"]
        if prov.get("analysis"):
            metadata["analysis"] = prov["analysis"]
        metadata["git"] = git_info
        if cwd_relative is not None:
            metadata["cwd"] = cwd_relative
        # Include persistent env vars in metadata for reproduction
        try:
            from ...config import load_config as _load_cfg

            _cfg = _load_cfg(start_dir=ctx.repo_root)
            _env = _cfg.get("env", {})
            if isinstance(_env, dict) and _env:
                metadata["env_vars"] = _env
        except Exception:
            pass

        metadata_json = json.dumps(metadata) if metadata else None

        # Collect telemetry
        telemetry_data = _collect_telemetry(ctx.repo_root, start_time, time.time())
        telemetry_json = json.dumps(telemetry_data) if telemetry_data else None

        stale_upstream = []
        stale_downstream = []

        with create_database_context(ctx.roar_dir) as db_ctx:
            job_id, job_uid = db_ctx.job_recording.record_job(
                command=shlex.join(ctx.command),
                timestamp=start_time,
                git_repo=git_repo,
                git_commit=git_commit,
                git_branch=git_branch,
                duration_seconds=tracer_result.duration,
                exit_code=tracer_result.exit_code,
                input_files=read_files,
                output_files=written_files,
                metadata=metadata_json,
                job_type=ctx.job_type,
                repo_root=ctx.repo_root,
                telemetry=telemetry_json,
                hash_algorithms=list(ctx.hash_algorithms),
            )

            # Get files with hashes for report
            written_file_info = db_ctx.jobs.get_outputs(job_id, db_ctx.artifacts)
            read_file_info = db_ctx.jobs.get_inputs(job_id, db_ctx.artifacts)

            # Check for stale steps
            session = db_ctx.sessions.get_active()
            if session:
                job = db_ctx.jobs.get(job_id)
                if job and job.get("step_number"):
                    step_num = job["step_number"]
                    stale = set(db_ctx.session_service.get_stale_steps(session["id"]))

                    # Check stale upstream
                    job_inputs = db_ctx.jobs.get_inputs(job_id, db_ctx.artifacts)
                    for inp in job_inputs:
                        artifact_hash = inp.get("artifact_hash")
                        if not artifact_hash:
                            continue
                        producer_jobs = db_ctx.artifacts.get_jobs(artifact_hash)
                        for pj in producer_jobs.get("produced_by", []):
                            producer_step = db_ctx.sessions.get_step_for_job(
                                session["id"], pj["id"]
                            )
                            if (
                                producer_step
                                and producer_step["step_number"] in stale
                                and producer_step["step_number"] not in stale_upstream
                            ):
                                stale_upstream.append(producer_step["step_number"])

                    # Check stale downstream
                    downstream = db_ctx.session_service.get_downstream_steps(
                        session["id"], step_num
                    )
                    stale_downstream = [s for s in downstream if s in stale]

        stale_upstream.sort()

        return job_id, job_uid, read_file_info, written_file_info, stale_upstream, stale_downstream

    def _backup_previous_outputs(self, ctx: RunContext) -> None:
        """
        Backup outputs from previous execution of the same command.

        When reversibility is enabled, this preserves files that were written
        by a previous job before they get overwritten by the current execution.
        Only backs up artifacts that are already tracked in the database.
        """
        from ...config import config_get
        from ...db.context import create_database_context
        from ...db.repositories.job import SQLAlchemyJobRepository

        if not config_get("reversible.enabled"):
            return

        try:
            with create_database_context(ctx.roar_dir) as db_ctx:
                # Find previous jobs with same command/script
                command_str = shlex.join(ctx.command)
                script = SQLAlchemyJobRepository._extract_script(command_str)
                if not script:
                    return

                jobs = db_ctx.jobs.get_by_script(script, limit=1)
                if not jobs:
                    return

                previous_job = jobs[0]
                outputs = db_ctx.jobs.get_outputs(previous_job["id"], db_ctx.artifacts)

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
                        # File is outside repo root, skip
                        continue

                    backup_path = ctx.roar_dir / "backups" / previous_job["job_uid"] / relative_path
                    backup_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(output_path, backup_path)
                    backup_count += 1

                if backup_count > 0:
                    self.logger.info(
                        "Backed up %d file(s) from previous run (job %s)",
                        backup_count,
                        previous_job["job_uid"][:8],
                    )

        except Exception as e:
            # Backup is best-effort, don't fail the run
            self.logger.warning("Failed to backup previous outputs: %s", e)

    def _cleanup_logs(self, tracer_log: str, inject_log: str) -> None:
        """Clean up temporary log files."""
        for log_file in [tracer_log, inject_log]:
            try:
                if log_file and os.path.exists(log_file):
                    os.remove(log_file)
            except OSError:
                pass
