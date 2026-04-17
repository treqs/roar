"""
Run coordinator service - main orchestrator for run/build execution.

Coordinates all services to execute commands with provenance tracking.
Follows SRP: coordinates, doesn't implement details.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
import time
from typing import TYPE_CHECKING, Any

from roar.execution.framework.contract import ROAR_EXECUTION_BACKEND_ENV
from roar.execution.recording import ExecutionJobRecorder

from .backup import PreviousOutputBackupService
from .tracer import TracerService

if TYPE_CHECKING:
    from ...core.interfaces.logger import ILogger
    from ...core.interfaces.presenter import IPresenter
    from ...core.models.run import RunContext, RunResult
    from ..cluster.proxy import ProxyService


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
        proxy_service: ProxyService | None = None,
        presenter: IPresenter | None = None,
        logger: ILogger | None = None,
        job_recorder: ExecutionJobRecorder | None = None,
        backup_service: PreviousOutputBackupService | None = None,
    ) -> None:
        """
        Initialize run coordinator.

        Args:
            tracer_service: Service for process tracing
            proxy_service: Optional S3 proxy service for lineage tracking
            presenter: Presenter for output
            logger: Logger for internal diagnostics
            job_recorder: Persistence service for job recording and stale analysis
            backup_service: Service for reversible output backup behavior
        """
        self._tracer = tracer_service or TracerService()
        self._proxy = proxy_service
        self._presenter = presenter
        self._logger = logger
        self._job_recorder = job_recorder or ExecutionJobRecorder()
        self._backup_service = backup_service or PreviousOutputBackupService()

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

        run_presenter = RunReportPresenter(self.presenter, quiet=ctx.quiet)

        # Start proxy if configured
        proxy_handle = None
        extra_env: dict[str, str] = {
            ROAR_EXECUTION_BACKEND_ENV: str(ctx.execution_backend),
        }
        s3_entries: list = []
        proxy_stopped = False
        if self._proxy:
            try:
                # Capture existing AWS_ENDPOINT_URL so the proxy can chain to it
                existing_endpoint = os.environ.get("AWS_ENDPOINT_URL")
                proxy_handle = self._proxy.start_for_run(
                    upstream_url=existing_endpoint,
                )
                extra_env["AWS_ENDPOINT_URL"] = f"http://127.0.0.1:{proxy_handle.port}"
                # Preserve the real upstream for cluster-backed submit paths.
                if existing_endpoint:
                    extra_env["ROAR_UPSTREAM_S3_ENDPOINT"] = existing_endpoint
                self.logger.debug("Proxy started on port %d", proxy_handle.port)
            except Exception as e:
                self.logger.warning("Failed to start proxy: %s", e)

        def stop_proxy_if_running() -> list:
            """Stop per-run proxy exactly once and return parsed entries."""
            nonlocal proxy_stopped, s3_entries

            if proxy_stopped:
                return s3_entries
            proxy_stopped = True

            if proxy_handle and self._proxy:
                try:
                    s3_entries = self._proxy.stop_for_run(proxy_handle)
                    self.logger.debug("Proxy stopped, collected %d S3 entries", len(s3_entries))
                except Exception as e:
                    self.logger.warning("Failed to stop proxy cleanly: %s", e)

            return s3_entries

        # Execute via tracer
        from ...core.exceptions import TracerNotFoundError

        # Announce trace is starting. We don't yet know the backend (it's
        # selected inside tracer.execute), so we just say "tracing"; the
        # backend shows up in the "trace done" line below.
        proxy_active = proxy_handle is not None
        run_presenter.trace_starting(ctx.tracer_mode, proxy_active)

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
            )
            run_presenter.trace_ended(tracer_result.duration, tracer_result.exit_code)
            self.logger.debug(
                "Tracer completed: exit_code=%d, duration=%.2fs, interrupted=%s",
                tracer_result.exit_code,
                tracer_result.duration,
                tracer_result.interrupted,
            )
        except TracerNotFoundError as e:
            from ...core.models.run import RunResult

            stop_proxy_if_running()
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
            stop_proxy_if_running()
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
            stop_proxy_if_running()
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
        )
        t_prov_end = time.perf_counter()
        n_read = len(prov.get("data", {}).get("read_files", []))
        n_written = len(prov.get("data", {}).get("written_files", []))
        self.logger.debug(
            "Provenance collected: read_files=%d, written_files=%d",
            n_read,
            n_written,
        )

        # Stop proxy and collect S3 entries before DB recording.
        s3_entries = stop_proxy_if_running()

        total_files = n_read + n_written
        with run_presenter.hashing(total=total_files or None):
            self.logger.debug("Recording job in database")
            t_record_start = time.perf_counter()
            job_id, job_uid, read_file_info, written_file_info, stale_upstream, stale_downstream = (
                self._record_job(
                    ctx,
                    prov,
                    tracer_result,
                    start_time,
                    is_build,
                    s3_entries,
                    run_job_uid=run_job_uid,
                )
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
        total_hash_bytes_early = sum(f.get("size") or 0 for f in written_file_info)
        hash_dur = t_record_end - t_record_start
        run_presenter.hashed(
            n_artifacts=len(read_file_info) + len(written_file_info),
            total_bytes=total_hash_bytes_early,
            duration=hash_dur,
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

        # Hash throughput: sum of all output sizes + record duration.
        total_hash_bytes = sum(f.get("size") or 0 for f in written_file_info)
        hash_duration = t_record_end - t_record_start

        # Git info (best-effort, never fail the run for this).
        git_branch, git_short_commit, git_clean = None, None, True
        try:
            git_branch = (
                subprocess.check_output(
                    ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                    stderr=subprocess.DEVNULL,
                    text=True,
                    cwd=ctx.repo_root,
                ).strip()
                or None
            )
            git_short_commit = (
                subprocess.check_output(
                    ["git", "rev-parse", "--short", "HEAD"],
                    stderr=subprocess.DEVNULL,
                    text=True,
                    cwd=ctx.repo_root,
                ).strip()
                or None
            )
        except Exception:
            pass

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
