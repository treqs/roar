"""
Run coordinator service - main orchestrator for run/build execution.

Coordinates all services to execute commands with provenance tracking.
Follows SRP: coordinates, doesn't implement details.
"""

from __future__ import annotations

import os
import secrets
import sys
import time
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from roar.execution.framework.contract import ROAR_EXECUTION_BACKEND_ENV
from roar.execution.recording import ExecutionJobRecorder

from .backup import PreviousOutputBackupService
from .resources import RuntimeObservationBundle, RuntimeResourceController
from .tracer import TracerService

if TYPE_CHECKING:
    from ...core.interfaces.logger import ILogger
    from ...core.interfaces.presenter import IPresenter
    from ...core.models.run import RunContext, RunResult
    from .resources import HostRuntimeResource


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
        runtime_resources: Sequence[HostRuntimeResource] | None = None,
        presenter: IPresenter | None = None,
        logger: ILogger | None = None,
        job_recorder: ExecutionJobRecorder | None = None,
        backup_service: PreviousOutputBackupService | None = None,
    ) -> None:
        """
        Initialize run coordinator.

        Args:
            tracer_service: Service for process tracing
            runtime_resources: Explicit runtime resources to start around host execution
            presenter: Presenter for output
            logger: Logger for internal diagnostics
            job_recorder: Persistence service for job recording and stale analysis
            backup_service: Service for reversible output backup behavior
        """
        self._tracer = tracer_service or TracerService()
        self._presenter = presenter
        self._logger = logger
        self._job_recorder = job_recorder or ExecutionJobRecorder()
        self._backup_service = backup_service or PreviousOutputBackupService()
        self._runtime_resources = RuntimeResourceController(
            resources=runtime_resources,
            logger=logger,
        )

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
            from ...core.logging import get_logger

            self._logger = get_logger()
        return self._logger

    def execute(self, ctx: RunContext) -> RunResult:
        """
        Execute a complete run with all tracking.

        Args:
            ctx: Run context with command and configuration

        Returns:
            RunResult with execution details
        """
        from .signal_handler import ProcessSignalHandler

        self.logger.debug(
            "RunCoordinator.execute started: command=%s, job_type=%s", ctx.command, ctx.job_type
        )
        start_time = time.time()
        run_job_uid = secrets.token_hex(4)
        is_build = ctx.job_type == "build"

        # Create signal handler
        signal_handler = ProcessSignalHandler(
            on_first_interrupt=lambda: self.logger.info(
                "Interrupted. Recording run... (Ctrl-C again to abort)"
            ),
        )

        # Backup previous outputs if reversibility is enabled
        self._backup_previous_outputs(ctx)

        # UX presenter for lifecycle output (stderr). Pipes through to existing
        # IPresenter for any legacy print_error calls.
        from ...presenters.run_report import RunReportPresenter

        run_presenter = RunReportPresenter(
            self.presenter,
            verbosity=ctx.verbosity,  # type: ignore[arg-type]
        )

        extra_env: dict[str, str] = {
            ROAR_EXECUTION_BACKEND_ENV: str(ctx.execution_backend),
        }
        runtime_observations = RuntimeObservationBundle()
        resource_env = self._runtime_resources.start_all(ctx, os.environ)
        extra_env.update(resource_env)

        def stop_runtime_resources(exit_code: int | None) -> RuntimeObservationBundle:
            """Stop runtime resources exactly once and return collected observations."""
            nonlocal runtime_observations
            runtime_observations = self._runtime_resources.stop_all(exit_code=exit_code)
            return runtime_observations

        # Execute via tracer
        from ...core.exceptions import (
            CommandNotFoundError,
            TracerNotFoundError,
            TracerPreflightError,
        )

        proxy_active = "proxy" in self._runtime_resources.active_resource_names()
        try:
            resolved_candidates = self._tracer.resolve_execution_candidates(
                ctx.command,
                tracer_mode_override=ctx.tracer_mode,
                fallback_enabled_override=ctx.tracer_fallback,
            )
        except CommandNotFoundError as e:
            # The command doesn't exist — nothing ran. Clean up and let the
            # caller print a one-line error instead of a job summary.
            stop_runtime_resources(e.exit_code)
            self.logger.debug("Command not found before execution: %s", e)
            raise
        except (TracerNotFoundError, TracerPreflightError) as e:
            from ...core.models.run import RunResult

            stop_runtime_resources(e.exit_code)
            self.logger.debug("Tracer unavailable before execution: %s", e)
            self.presenter.print_error(e.message)
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

        resolved_mode = resolved_candidates[0][0] if resolved_candidates else ctx.tracer_mode

        run_presenter.trace_starting(resolved_mode, proxy_active)

        self.logger.debug("Starting tracer execution")
        try:
            tracer_result = self._tracer.execute(
                ctx.command,
                ctx.roar_dir,
                signal_handler,
                extra_env=extra_env,
                job_id=run_job_uid,
                tracer_mode_override=ctx.tracer_mode,
                fallback_enabled_override=ctx.tracer_fallback,
                candidates_override=resolved_candidates,
            )
            run_presenter.trace_ended(tracer_result.duration, tracer_result.exit_code)
            self.logger.debug(
                "Tracer completed: exit_code=%d, duration=%.2fs, interrupted=%s",
                tracer_result.exit_code,
                tracer_result.duration,
                tracer_result.interrupted,
            )
        except (TracerNotFoundError, TracerPreflightError) as e:
            from ...core.models.run import RunResult

            stop_runtime_resources(e.exit_code)
            self.logger.debug("Tracer execution aborted before tracing: %s", e)
            self.presenter.print_error(e.message)
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
        except Exception:
            stop_runtime_resources(None)
            raise

        # Check if we should abort (double Ctrl-C)
        if signal_handler.should_abort():
            stop_runtime_resources(tracer_result.exit_code)
            self._cleanup_logs(tracer_result.tracer_log_path, tracer_result.inject_log_path)
            sys.exit(130)

        # Heavy imports deferred to after tracer fork
        from ...core.bootstrap import bootstrap
        from ...core.models.run import RunResult
        from ...integrations.config import load_config
        from ..provenance import ProvenanceService

        emit_timing = os.environ.get("ROAR_TIMING") == "1"
        t_postrun_start = time.perf_counter()

        bootstrap(ctx.roar_dir)
        config = load_config(start_dir=ctx.repo_root)

        # Check if tracer log exists
        if not os.path.exists(tracer_result.tracer_log_path):
            self.logger.warning("Tracer log not found at %s", tracer_result.tracer_log_path)
            self.logger.warning("The tracer may have failed to start. Run was not recorded.")
            stop_runtime_resources(tracer_result.exit_code)
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

        # Collect provenance first (fast) so we know total file count for the
        # hashing spinner.
        self.logger.debug("Collecting provenance data")
        inject_log = (
            tracer_result.inject_log_path if os.path.exists(tracer_result.inject_log_path) else None
        )
        roar_dir = os.path.join(ctx.repo_root, ".roar")
        provenance_service = ProvenanceService(cache_dir=roar_dir)
        t_prov_start = time.perf_counter()
        prov = provenance_service.collect(
            ctx.repo_root,
            tracer_result.tracer_log_path,
            inject_log,
            config,
            collect_dropped_paths=(ctx.verbosity == "debug"),
            command=list(ctx.command),
        )
        t_prov_end = time.perf_counter()
        n_read = len(prov.get("data", {}).get("read_files", []))
        n_written = len(prov.get("data", {}).get("written_files", []))
        self.logger.debug(
            "Provenance collected: read_files=%d, written_files=%d",
            n_read,
            n_written,
        )

        # Stop runtime resources and collect observations before DB recording.
        runtime_observations = stop_runtime_resources(tracer_result.exit_code)

        total_files = n_read + n_written
        with run_presenter.hashing(total=total_files or None):
            self.logger.debug("Recording job in database")
            t_record_start = time.perf_counter()
            (
                job_id,
                job_uid,
                read_file_info,
                written_file_info,
                stale_upstream,
                stale_downstream,
                dag_stats,
            ) = self._record_job(
                ctx,
                prov,
                tracer_result,
                start_time,
                is_build,
                list(runtime_observations.s3_entries),
                run_job_uid=run_job_uid,
            )
            t_record_end = time.perf_counter()
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

        # "hashed" throughput line.
        total_hash_bytes = sum(f.get("size") or 0 for f in written_file_info)
        hash_duration = t_record_end - t_record_start
        run_presenter.hashed(
            n_artifacts=len(read_file_info) + len(written_file_info),
            total_bytes=total_hash_bytes,
            duration=hash_duration,
        )
        run_presenter.lineage_captured()

        t_postrun_end = time.perf_counter()

        if emit_timing:
            import json as _json

            n_inputs = len(prov.get("data", {}).get("read_files", []))
            n_outputs = len(prov.get("data", {}).get("written_files", []))
            timing = {
                "roar_timing": True,
                "tracer_ms": round(tracer_result.duration * 1000, 2),
                "postrun_ms": round((t_postrun_end - t_postrun_start) * 1000, 2),
                "provenance_ms": round((t_prov_end - t_prov_start) * 1000, 2),
                "record_ms": round((t_record_end - t_record_start) * 1000, 2),
                "n_inputs": n_inputs,
                "n_outputs": n_outputs,
            }
            print(f"ROAR_TIMING:{_json.dumps(timing)}", file=sys.stderr, flush=True)

        self.logger.debug(
            "RunCoordinator.execute completed: exit_code=%d, duration=%.2fs",
            tracer_result.exit_code,
            tracer_result.duration,
        )
        # UX metadata for the new run presenter.
        exec_packages = prov.get("executables", {}).get("packages", {}) or {}
        pip_count = len(exec_packages.get("pip", {}) or {})
        dpkg_count = len(exec_packages.get("dpkg", {}) or {})
        env_count = len(prov.get("runtime", {}).get("env_vars", {}) or {})
        post_duration = t_postrun_end - t_postrun_start
        backend_name = getattr(tracer_result, "backend", None)
        if not isinstance(backend_name, str):
            backend_name = None

        # Git info — reuse what provenance already collected instead of
        # spawning new subprocess calls.
        git_branch, git_short_commit, git_clean = None, None, True
        try:
            prov_git = prov.get("executables", {}).get("code", {}).get("git", {})
            git_branch = prov_git.get("branch") or None
            full_commit = prov_git.get("commit") or ""
            git_short_commit = full_commit[:7] if full_commit else None
            git_clean = prov_git.get("clean", True)
        except Exception:
            pass

        # DAG stats + parent job lookup — collected inside _record_job's
        # DB context to avoid opening a 2nd SQLAlchemy session (~250ms import cost).
        dag_jobs = dag_stats.get("dag_jobs", 0)
        dag_artifacts = dag_stats.get("dag_artifacts", 0)
        dag_depth = dag_stats.get("dag_depth", 0)
        step_number = dag_stats.get("step_number")

        prov_data = prov.get("data", {}) if isinstance(prov, dict) else {}
        filter_counts = prov_data.get("filter_counts") or {}
        dropped_paths = prov_data.get("dropped_paths") or {}
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
            backend=backend_name,
            post_duration=post_duration,
            proxy_active=proxy_active,
            pip_count=pip_count,
            dpkg_count=dpkg_count,
            env_count=env_count,
            git_branch=git_branch,
            git_short_commit=git_short_commit,
            git_clean=git_clean,
            total_hash_bytes=total_hash_bytes,
            hash_duration=hash_duration,
            dag_jobs=dag_jobs,
            dag_artifacts=dag_artifacts,
            dag_depth=dag_depth,
            step_number=step_number,
            filter_counts=filter_counts if isinstance(filter_counts, dict) else {},
            dropped_paths=dropped_paths if isinstance(dropped_paths, dict) else {},
        )

    def _record_job(
        self,
        ctx: RunContext,
        prov: dict[str, Any],
        tracer_result,
        start_time: float,
        is_build: bool,
        s3_entries: list | None = None,
        run_job_uid: str | None = None,
    ) -> tuple:
        """Record job in database and return file info.

        Thin wrapper kept for test seam compatibility.
        """
        return self._job_recorder.record(
            ctx=ctx,
            prov=prov,
            tracer_result=tracer_result,
            start_time=start_time,
            is_build=is_build,
            s3_entries=s3_entries,
            run_job_uid=run_job_uid,
        )

    def _backup_previous_outputs(self, ctx: RunContext) -> None:
        """
        Backup outputs from previous execution of the same command.

        When reversibility is enabled, this preserves files that were written
        by a previous job before they get overwritten by the current execution.
        Only backs up artifacts that are already tracked in the database.
        """
        self._backup_service.backup_previous_outputs(ctx, self.logger)

    def _cleanup_logs(self, tracer_log: str, inject_log: str) -> None:
        """Clean up temporary log files."""
        for log_file in [tracer_log, inject_log]:
            try:
                if log_file and os.path.exists(log_file):
                    os.remove(log_file)
            except OSError:
                pass
