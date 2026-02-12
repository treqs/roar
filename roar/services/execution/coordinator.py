"""
Run coordinator service - main orchestrator for run/build execution.

Coordinates all services to execute commands with provenance tracking.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ...core.exceptions import TracerNotFoundError
from ...core.interfaces.logger import ILogger
from ...core.interfaces.presenter import IPresenter
from ...core.interfaces.run import RunContext, RunResult
from .backup import PreviousOutputBackupService
from .job_recording import ExecutionJobRecorder
from .ray_lineage import RayLineageRecorder
from .signal_handler import ProcessSignalHandler
from .tracer import TracerService

if TYPE_CHECKING:
    from .proxy import ProxyService


@dataclass(frozen=True)
class _DirectExecutionResult:
    exit_code: int
    duration: float
    interrupted: bool


class RunCoordinator:
    """
    Orchestrates the complete run lifecycle.

    Follows SRP: coordinates services and control flow without owning tracer,
    provenance, or persistence implementation details.
    """

    def __init__(
        self,
        tracer_service: TracerService | None = None,
        proxy_service: ProxyService | None = None,
        presenter: IPresenter | None = None,
        logger: ILogger | None = None,
        job_recorder: ExecutionJobRecorder | None = None,
        ray_lineage_recorder: RayLineageRecorder | None = None,
        backup_service: PreviousOutputBackupService | None = None,
    ) -> None:
        self._tracer = tracer_service or TracerService()
        self._proxy = proxy_service
        self._presenter = presenter
        self._logger = logger
        self._job_recorder = job_recorder or ExecutionJobRecorder()
        self._ray_lineage_recorder = ray_lineage_recorder or RayLineageRecorder()
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
        Execute a complete run with provenance tracking.

        Supports local tracer execution and distributed Ray execution.
        """
        self.logger.debug(
            "RunCoordinator.execute started: command=%s, job_type=%s, backend=%s",
            ctx.command,
            ctx.job_type,
            getattr(ctx, "execution_backend", "local"),
        )
        start_time = time.time()
        is_build = ctx.job_type == "build"

        signal_handler = ProcessSignalHandler(
            on_first_interrupt=lambda: self.logger.info(
                "Interrupted. Recording run... (Ctrl-C again to abort)"
            ),
        )

        self._backup_previous_outputs(ctx)

        backend_value = getattr(ctx, "execution_backend", "local")
        backend = backend_value if isinstance(backend_value, str) else "local"
        if backend not in {"local", "ray"}:
            backend = "local"
        if backend == "ray":
            return self._execute_ray_backend(
                ctx=ctx,
                start_time=start_time,
                is_build=is_build,
                signal_handler=signal_handler,
            )
        return self._execute_local_backend(
            ctx=ctx,
            start_time=start_time,
            is_build=is_build,
            signal_handler=signal_handler,
        )

    def _execute_local_backend(
        self,
        ctx: RunContext,
        start_time: float,
        is_build: bool,
        signal_handler: ProcessSignalHandler,
    ) -> RunResult:
        from ...config import load_config
        from .provenance import ProvenanceService

        proxy_handle = None
        extra_env: dict[str, str] | None = None
        s3_entries: list = []
        proxy_stopped = False
        if self._proxy:
            try:
                existing_endpoint = os.environ.get("AWS_ENDPOINT_URL")
                proxy_handle = self._proxy.start_for_run(upstream_url=existing_endpoint)
                extra_env = {"AWS_ENDPOINT_URL": f"http://127.0.0.1:{proxy_handle.port}"}
                self.logger.debug("Proxy started on port %d", proxy_handle.port)
            except Exception as exc:
                self.logger.warning("Failed to start proxy: %s", exc)

        def stop_proxy_if_running() -> list:
            nonlocal proxy_stopped, s3_entries

            if proxy_stopped:
                return s3_entries
            proxy_stopped = True

            if proxy_handle and self._proxy:
                try:
                    s3_entries = self._proxy.stop_for_run(proxy_handle)
                    self.logger.debug("Proxy stopped, collected %d S3 entries", len(s3_entries))
                except Exception as exc:
                    self.logger.warning("Failed to stop proxy cleanly: %s", exc)
            return s3_entries

        self.logger.debug("Starting local tracer execution")
        try:
            tracer_result = self._tracer.execute(
                ctx.command,
                ctx.roar_dir,
                signal_handler,
                extra_env=extra_env,
                tracer_mode_override=ctx.tracer_mode,
                fallback_enabled_override=ctx.tracer_fallback,
            )
            self.logger.debug(
                "Tracer completed: exit_code=%d, duration=%.2fs, interrupted=%s",
                tracer_result.exit_code,
                tracer_result.duration,
                tracer_result.interrupted,
            )
        except TracerNotFoundError as exc:
            stop_proxy_if_running()
            self.logger.debug("Tracer not found: %s", exc)
            self.presenter.print_error(str(exc))
            return RunResult(
                exit_code=exc.exit_code,
                job_id=0,
                job_uid="000000",
                duration=0,
                inputs=[],
                outputs=[],
                interrupted=False,
                is_build=is_build,
            )

        if signal_handler.should_abort():
            stop_proxy_if_running()
            self._cleanup_logs(tracer_result.tracer_log_path, tracer_result.inject_log_path)
            sys.exit(130)

        config = load_config(start_dir=ctx.repo_root)
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

        self.logger.debug("Collecting local provenance data")
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

        s3_entries = stop_proxy_if_running()
        self.logger.debug("Recording local job in database")
        job_id, job_uid, read_file_info, written_file_info, stale_upstream, stale_downstream = (
            self._record_job(ctx, prov, tracer_result, start_time, is_build, s3_entries)
        )

        self._cleanup_logs(tracer_result.tracer_log_path, tracer_result.inject_log_path)
        self.logger.debug(
            "Local backend completed: exit_code=%d, duration=%.2fs",
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

    def _execute_ray_backend(
        self,
        ctx: RunContext,
        start_time: float,
        is_build: bool,
        signal_handler: ProcessSignalHandler,
    ) -> RunResult:
        from ...ray import (
            ROAR_DISTRIBUTED_BACKEND_ENV,
            ROAR_RAY_ADDRESS_ENV,
            ROAR_RAY_LINEAGE_ACTOR_ENV,
            ROAR_RAY_NAMESPACE_ENV,
            ROAR_RAY_RUN_ID_ENV,
            create_lineage_actor,
            destroy_lineage_actor,
            fetch_lineage_events,
            is_ray_available,
        )

        if not is_ray_available():
            self.presenter.print_error(
                "Ray backend selected but the 'ray' package is not available. "
                "Install ray to use distributed execution."
            )
            return RunResult(
                exit_code=1,
                job_id=0,
                job_uid="000000",
                duration=0,
                inputs=[],
                outputs=[],
                interrupted=False,
                is_build=is_build,
            )

        proxy_handle = None
        base_extra_env: dict[str, str] = {}
        s3_entries: list = []
        proxy_stopped = False
        if self._proxy:
            try:
                existing_endpoint = os.environ.get("AWS_ENDPOINT_URL")
                proxy_handle = self._proxy.start_for_run(upstream_url=existing_endpoint)
                base_extra_env["AWS_ENDPOINT_URL"] = f"http://127.0.0.1:{proxy_handle.port}"
                self.logger.debug("Proxy started on port %d", proxy_handle.port)
            except Exception as exc:
                self.logger.warning("Failed to start proxy: %s", exc)

        def stop_proxy_if_running() -> list:
            nonlocal proxy_stopped, s3_entries

            if proxy_stopped:
                return s3_entries
            proxy_stopped = True
            if proxy_handle and self._proxy:
                try:
                    s3_entries = self._proxy.stop_for_run(proxy_handle)
                    self.logger.debug("Proxy stopped, collected %d S3 entries", len(s3_entries))
                except Exception as exc:
                    self.logger.warning("Failed to stop proxy cleanly: %s", exc)
            return s3_entries

        run_id = secrets.token_hex(12)
        actor_name = f"roar-lineage-{run_id}"
        namespace = ctx.ray_namespace or "roar"
        address = ctx.ray_address
        direct_result = _DirectExecutionResult(exit_code=1, duration=0.0, interrupted=False)

        created, create_error = create_lineage_actor(
            actor_name=actor_name,
            namespace=namespace,
            address=address,
        )
        if not created:
            stop_proxy_if_running()
            self.presenter.print_error(f"Failed to initialize Ray lineage actor: {create_error}")
            return RunResult(
                exit_code=1,
                job_id=0,
                job_uid="000000",
                duration=0,
                inputs=[],
                outputs=[],
                interrupted=False,
                is_build=is_build,
            )

        events: list[dict[str, Any]] = []
        fetch_error: str | None = None
        try:
            extra_env = dict(base_extra_env)
            extra_env[ROAR_DISTRIBUTED_BACKEND_ENV] = "ray"
            extra_env[ROAR_RAY_LINEAGE_ACTOR_ENV] = actor_name
            extra_env[ROAR_RAY_NAMESPACE_ENV] = namespace
            extra_env[ROAR_RAY_RUN_ID_ENV] = run_id
            if address:
                extra_env[ROAR_RAY_ADDRESS_ENV] = address

            direct_result = self._execute_direct_command(
                command=ctx.command,
                signal_handler=signal_handler,
                extra_env=extra_env,
                repo_root=ctx.repo_root,
            )

            events, fetch_error = fetch_lineage_events(
                actor_name=actor_name,
                namespace=namespace,
                address=address,
            )
            if fetch_error:
                self.logger.warning("Failed to fetch Ray lineage events: %s", fetch_error)
        finally:
            destroyed, destroy_error = destroy_lineage_actor(
                actor_name=actor_name,
                namespace=namespace,
                address=address,
            )
            if not destroyed and destroy_error:
                self.logger.warning("Failed to destroy Ray lineage actor: %s", destroy_error)

        if signal_handler.should_abort():
            stop_proxy_if_running()
            sys.exit(130)

        s3_entries = stop_proxy_if_running()
        self.logger.debug(
            "Ray execution completed: exit_code=%d, events=%d",
            direct_result.exit_code,
            len(events),
        )

        (
            job_id,
            job_uid,
            read_file_info,
            written_file_info,
            stale_upstream,
            stale_downstream,
        ) = self._ray_lineage_recorder.record(
            ctx=ctx,
            events=events,
            execution_result=direct_result,
            start_time=start_time,
            s3_entries=s3_entries,
            run_id=run_id,
        )

        if fetch_error:
            self.presenter.print_error(
                "Ray lineage capture completed with warnings: distributed task events "
                "could not be fully fetched."
            )

        return RunResult(
            exit_code=direct_result.exit_code,
            job_id=job_id,
            job_uid=job_uid,
            duration=direct_result.duration,
            inputs=read_file_info,
            outputs=written_file_info,
            interrupted=direct_result.interrupted,
            is_build=is_build,
            stale_upstream=stale_upstream,
            stale_downstream=stale_downstream,
        )

    def _execute_direct_command(
        self,
        command: list[str],
        signal_handler: ProcessSignalHandler,
        extra_env: dict[str, str] | None,
        repo_root: str | None,
    ) -> _DirectExecutionResult:
        from ...config import load_config

        env = dict(os.environ)
        try:
            config = load_config(start_dir=repo_root)
            config_env = config.get("env", {})
            if isinstance(config_env, dict):
                env.update({k: str(v) for k, v in config_env.items()})
        except Exception:
            pass

        if extra_env:
            env.update(extra_env)

        start = time.time()
        signal_handler.install()
        exit_code = 1
        proc: subprocess.Popen[Any] | None = None
        try:
            proc = subprocess.Popen(command, env=env)
            exit_code = proc.wait()
        except KeyboardInterrupt:
            if proc is not None:
                exit_code = proc.wait()
            else:
                exit_code = 130
        except OSError as exc:
            self.logger.warning("Failed to start command for Ray backend: %s", exc)
            exit_code = 1
        finally:
            signal_handler.restore()

        return _DirectExecutionResult(
            exit_code=exit_code,
            duration=max(0.0, time.time() - start),
            interrupted=signal_handler.is_interrupted(),
        )

    def _record_job(
        self,
        ctx: RunContext,
        prov: dict[str, Any],
        tracer_result: Any,
        start_time: float,
        is_build: bool,
        s3_entries: list | None = None,
    ) -> tuple:
        """Record local traced job and return reporting tuple."""
        return self._job_recorder.record(
            ctx=ctx,
            prov=prov,
            tracer_result=tracer_result,
            start_time=start_time,
            is_build=is_build,
            s3_entries=s3_entries,
        )

    def _backup_previous_outputs(self, ctx: RunContext) -> None:
        """Backup outputs from previous execution of the same command."""
        self._backup_service.backup_previous_outputs(ctx, self.logger)

    def _cleanup_logs(self, tracer_log: str, inject_log: str) -> None:
        """Clean up temporary log files."""
        for log_file in [tracer_log, inject_log]:
            try:
                if log_file and os.path.exists(log_file):
                    os.remove(log_file)
            except OSError:
                pass
