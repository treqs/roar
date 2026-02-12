"""
Job recording helpers for run/build execution.

This module extracts persistence-oriented responsibilities from RunCoordinator:
- metadata and telemetry shaping
- job recording
- stale-step analysis
- proxy S3 artifact registration
"""

from __future__ import annotations

import json
import shlex
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ...core.interfaces.run import RunContext
    from ..execution.proxy import S3LogEntry


def _get_logger():
    from ...core.logging import get_logger

    return get_logger()


def collect_telemetry(
    repo_root: str, start_time: float, end_time: float, allow_incomplete: bool = False
) -> dict[str, Any] | None:
    """Collect telemetry from registered providers (best effort)."""
    from ...core.container import get_container

    telemetry_data: dict[str, Any] = {}
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
        # Telemetry is best-effort and should never fail the run.
        _get_logger().debug("Failed to collect telemetry: %s", e)

    return telemetry_data if telemetry_data else None


class StalenessAnalyzer:
    """Compute stale upstream/downstream step numbers for a recorded job."""

    def analyze(self, db_ctx: Any, session_id: int, job_id: int) -> tuple[list[int], list[int]]:
        stale_upstream: list[int] = []
        stale_downstream: list[int] = []

        job = db_ctx.jobs.get(job_id)
        if not job or not job.get("step_number"):
            return stale_upstream, stale_downstream

        step_num = job["step_number"]
        stale = set(db_ctx.session_service.get_stale_steps(session_id))

        # Check stale upstream by walking producer steps for each input artifact.
        job_inputs = db_ctx.jobs.get_inputs(job_id)
        for inp in job_inputs:
            artifact_hash = inp.get("artifact_hash")
            if not artifact_hash:
                continue
            producer_jobs = db_ctx.artifacts.get_jobs(artifact_hash)
            for producer_job in producer_jobs.get("produced_by", []):
                producer_step = db_ctx.sessions.get_step_for_job(session_id, producer_job["id"])
                if (
                    producer_step
                    and producer_step["step_number"] in stale
                    and producer_step["step_number"] not in stale_upstream
                ):
                    stale_upstream.append(producer_step["step_number"])

        # Check stale downstream from current step.
        downstream = db_ctx.session_service.get_downstream_steps(session_id, step_num)
        stale_downstream = [s for s in downstream if s in stale]
        stale_upstream.sort()
        return stale_upstream, stale_downstream


class ProxyArtifactRegistrar:
    """Attach proxy-captured S3 artifacts as job inputs/outputs."""

    _READ_OPS = frozenset({"GetObject"})
    _WRITE_OPS = frozenset({"PutObject", "CompleteMultipartUpload"})
    _TRACKED_OPS = _READ_OPS | _WRITE_OPS

    def register(self, db_ctx: Any, job_id: int, s3_entries: list[S3LogEntry] | None) -> None:
        if not s3_entries:
            return

        for entry in s3_entries:
            if entry.operation not in self._TRACKED_OPS or not entry.etag:
                continue

            s3_url = f"s3://{entry.bucket}/{entry.key}"
            artifact_id, _ = db_ctx.artifacts.register(
                hashes={"etag": entry.etag},
                size=entry.size_bytes or 0,
                path=s3_url,
                source_type="s3",
                source_url=s3_url,
            )
            if entry.operation in self._READ_OPS:
                db_ctx.jobs.add_input(job_id, artifact_id, s3_url, byte_ranges=entry.byte_ranges)
            else:
                db_ctx.jobs.add_output(job_id, artifact_id, s3_url)


class ExecutionJobRecorder:
    """Persist a traced execution and return reporting payload pieces."""

    def __init__(
        self,
        telemetry_collector: Callable[[str, float, float, bool], dict[str, Any] | None]
        | None = None,
        staleness_analyzer: StalenessAnalyzer | None = None,
        proxy_artifact_registrar: ProxyArtifactRegistrar | None = None,
    ) -> None:
        self._telemetry_collector = telemetry_collector or collect_telemetry
        self._staleness_analyzer = staleness_analyzer or StalenessAnalyzer()
        self._proxy_artifact_registrar = proxy_artifact_registrar or ProxyArtifactRegistrar()

    def record(
        self,
        ctx: RunContext,
        prov: dict[str, Any],
        tracer_result: Any,
        start_time: float,
        is_build: bool,
        s3_entries: list[S3LogEntry] | None = None,
    ) -> tuple[int, str, list[dict[str, Any]], list[dict[str, Any]], list[int], list[int]]:
        """Record job and return tuple expected by RunCoordinator."""
        from ...db.context import create_database_context

        if is_build:
            written_files: list[str] = []
            read_files: list[str] = []
        else:
            written_files = self._normalize_paths(prov.get("data", {}).get("written_files", []))
            read_files = self._normalize_paths(prov.get("data", {}).get("read_files", []))

        git_info = prov.get("executables", {}).get("code", {}).get("git", {})
        git_commit = git_info.get("commit")
        git_branch = git_info.get("branch")
        git_repo = git_info.get("remote_url")

        metadata_json = self._build_metadata_json(ctx, prov, git_info)
        telemetry_json = self._build_telemetry_json(ctx.repo_root, start_time)

        stale_upstream: list[int] = []
        stale_downstream: list[int] = []
        with create_database_context(ctx.roar_dir) as db_ctx:
            job_id, job_uid = db_ctx.job_recording.record_job(
                command=shlex.join(ctx.command),
                timestamp=start_time,
                step_name=ctx.step_name,
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

            written_file_info = db_ctx.jobs.get_outputs(job_id)
            read_file_info = db_ctx.jobs.get_inputs(job_id)

            session = db_ctx.sessions.get_active()
            if session:
                stale_upstream, stale_downstream = self._staleness_analyzer.analyze(
                    db_ctx, session["id"], job_id
                )

            self._proxy_artifact_registrar.register(db_ctx, job_id, s3_entries)

        return job_id, job_uid, read_file_info, written_file_info, stale_upstream, stale_downstream

    def _build_metadata_json(
        self,
        ctx: RunContext,
        prov: dict[str, Any],
        git_info: dict[str, Any],
    ) -> str | None:
        metadata: dict[str, Any] = {}

        if prov.get("executables", {}).get("packages"):
            metadata["packages"] = prov["executables"]["packages"]
        if prov.get("runtime"):
            metadata["runtime"] = prov["runtime"]
        if prov.get("analysis"):
            metadata["analysis"] = prov["analysis"]
        metadata["git"] = git_info

        cwd_relative = self._compute_cwd_relative(ctx.repo_root)
        if cwd_relative is not None:
            metadata["cwd"] = cwd_relative

        # Include persistent env vars in metadata for reproduction.
        try:
            from ...config import load_config

            config = load_config(start_dir=ctx.repo_root)
            env_vars = config.get("env", {})
            if isinstance(env_vars, dict) and env_vars:
                metadata["env_vars"] = env_vars
        except Exception:
            pass

        return json.dumps(metadata) if metadata else None

    def _build_telemetry_json(self, repo_root: str, start_time: float) -> str | None:
        telemetry_data = self._telemetry_collector(repo_root, start_time, time.time(), False)
        return json.dumps(telemetry_data) if telemetry_data else None

    @staticmethod
    def _normalize_paths(raw_files: Any) -> list[str]:
        """Normalize provenance file lists to plain path strings."""
        normalized: list[str] = []
        if not isinstance(raw_files, list):
            return normalized
        for item in raw_files:
            if isinstance(item, str):
                normalized.append(item)
            elif isinstance(item, dict) and isinstance(item.get("path"), str):
                normalized.append(item["path"])
        return normalized

    @staticmethod
    def _compute_cwd_relative(repo_root: str) -> str | None:
        try:
            relative = str(Path.cwd().relative_to(Path(repo_root)))
            return "" if relative == "." else relative
        except ValueError:
            return None
