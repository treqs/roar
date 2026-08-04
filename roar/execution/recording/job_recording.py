"""
Job recording helpers for local execution persistence.

This module extracts persistence-oriented responsibilities from higher-level
execution/application orchestration:
- metadata and telemetry shaping
- job recording
- stale-step analysis
- proxy S3 artifact registration
- dataset identifier inference from observed paths
"""

from __future__ import annotations

import json
import os
import re
import shlex
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, cast

from ... import __version__ as _ROAR_VERSION
from ...application.system_labels import refresh_job_system_labels
from .dataset_identifier import DatasetIdentifierInferer

if TYPE_CHECKING:
    from ...core.models.run import RunContext
    from ..cluster.proxy import S3LogEntry


def _get_logger():
    from ...core.logging import get_logger

    return get_logger()


def collect_telemetry(
    repo_root: str, start_time: float, end_time: float, allow_incomplete: bool = False
) -> dict[str, Any] | None:
    """Collect telemetry from registered providers (best effort)."""
    from ...integrations import get_all_telemetry_providers

    telemetry_data: dict[str, Any] = {}
    try:
        providers = get_all_telemetry_providers()
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


@dataclass(frozen=True)
class LocalRecordedArtifact:
    """Precomputed artifact facts for local job recording."""

    path: str
    hashes: dict[str, str]
    size: int
    source_type: str | None = None
    source_url: str | None = None
    metadata: str | None = None
    byte_ranges: list[list[int]] | None = None


class LocalJobRecorder:
    """Persist a local job from precomputed artifact facts."""

    def record(
        self,
        db_ctx: Any,
        *,
        command: str,
        timestamp: float,
        metadata: str | None,
        execution_backend: str,
        execution_role: str,
        job_type: str,
        output_artifacts: list[LocalRecordedArtifact],
        input_artifacts: list[LocalRecordedArtifact] | None = None,
        duration_seconds: float | None = None,
        exit_code: int | None = None,
        session_id: int | None = None,
        job_uid: str | None = None,
        git_commit: str | None = None,
        git_branch: str | None = None,
        git_repo: str | None = None,
        step_name: str | None = None,
    ) -> tuple[int, str]:
        """Create a job and link precomputed input/output artifacts."""
        resolved_session_id = session_id
        if resolved_session_id is None:
            resolved_session_id = int(db_ctx.sessions.get_or_create_active())

        step_number = db_ctx.sessions.get_next_step_number(resolved_session_id)
        job_id, recorded_job_uid = db_ctx.jobs.create(
            command=command,
            timestamp=timestamp,
            job_uid=job_uid,
            session_id=resolved_session_id,
            step_number=step_number,
            step_name=step_name,
            metadata=metadata,
            execution_backend=execution_backend,
            execution_role=execution_role,
            job_type=job_type,
            exit_code=exit_code,
            duration_seconds=duration_seconds,
            git_commit=git_commit,
            git_branch=git_branch,
            git_repo=git_repo,
        )

        self._register_artifacts(
            db_ctx=db_ctx,
            job_id=job_id,
            artifacts=input_artifacts or [],
            is_input=True,
        )
        self._register_artifacts(
            db_ctx=db_ctx,
            job_id=job_id,
            artifacts=output_artifacts,
            is_input=False,
        )

        refresh_job_system_labels(
            db_ctx,
            job_id=job_id,
            job=cast(Any, db_ctx.jobs).get(job_id),
        )

        if step_name:
            db_ctx.job_recording._record_step_name_label(job_id, step_name)

        return job_id, recorded_job_uid

    @staticmethod
    def _register_artifacts(
        *,
        db_ctx: Any,
        job_id: int,
        artifacts: list[LocalRecordedArtifact],
        is_input: bool,
    ) -> None:
        for artifact in artifacts:
            artifact_id, _created = db_ctx.artifacts.register(
                hashes=artifact.hashes,
                size=artifact.size,
                path=artifact.path,
                source_type=artifact.source_type,
                source_url=artifact.source_url,
                metadata=artifact.metadata,
            )
            if is_input:
                db_ctx.jobs.add_input(
                    job_id,
                    artifact_id,
                    artifact.path,
                    byte_ranges=artifact.byte_ranges,
                )
            else:
                db_ctx.jobs.add_output(
                    job_id,
                    artifact_id,
                    artifact.path,
                    byte_ranges=artifact.byte_ranges,
                )


class ExecutionJobRecorder:
    """Persist a traced execution and return reporting payload pieces."""

    _DATASET_HINT_FLAGS: ClassVar[set[str]] = {
        "--input",
        "--input-dir",
        "--input-path",
        "--inputs",
        "--dataset",
        "--dataset-dir",
        "--data-dir",
        "--data-root",
        "--source",
        "--source-dir",
        "--output-dir",
    }
    _LIST_SEPARATORS = re.compile(r"[,\s]+")

    def __init__(
        self,
        telemetry_collector: Callable[[str, float, float, bool], dict[str, Any] | None]
        | None = None,
        staleness_analyzer: StalenessAnalyzer | None = None,
        proxy_artifact_registrar: ProxyArtifactRegistrar | None = None,
        dataset_identifier_inferer: DatasetIdentifierInferer | None = None,
    ) -> None:
        self._telemetry_collector = telemetry_collector or collect_telemetry
        self._staleness_analyzer = staleness_analyzer or StalenessAnalyzer()
        self._proxy_artifact_registrar = proxy_artifact_registrar or ProxyArtifactRegistrar()
        self._dataset_identifier_inferer = dataset_identifier_inferer or DatasetIdentifierInferer()

    def record(
        self,
        ctx: RunContext,
        prov: dict[str, Any],
        tracer_result: Any,
        start_time: float,
        is_build: bool,
        s3_entries: list[S3LogEntry] | None = None,
        run_job_uid: str | None = None,
    ) -> tuple[
        int, str, list[dict[str, Any]], list[dict[str, Any]], list[int], list[int], dict[str, Any]
    ]:
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

        has_files = bool(read_files or written_files)
        dataset_hint_paths = self._extract_dataset_hint_paths(ctx.command, ctx.repo_root)
        telemetry_json = (
            self._build_telemetry_json(ctx.repo_root, start_time) if has_files else None
        )

        stale_upstream: list[int] = []
        stale_downstream: list[int] = []
        with create_database_context(ctx.roar_dir) as db_ctx:
            session_window_paths = self._collect_session_window_paths(db_ctx) if has_files else []
            metadata_json = self._build_metadata_json(
                ctx,
                prov,
                git_info,
                read_files,
                written_files,
                session_window_paths=session_window_paths,
                dataset_hint_paths=dataset_hint_paths,
            )
            job_id, job_uid = db_ctx.job_recording.record_job(
                command=shlex.join(ctx.command),
                timestamp=start_time,
                job_uid=run_job_uid,
                step_name=ctx.step_name,
                git_repo=git_repo,
                git_commit=git_commit,
                git_branch=git_branch,
                duration_seconds=tracer_result.duration,
                exit_code=tracer_result.exit_code,
                input_files=read_files,
                output_files=written_files,
                metadata=metadata_json,
                execution_backend=ctx.execution_backend,
                execution_role=ctx.execution_role,
                job_type=ctx.job_type,
                repo_root=ctx.repo_root,
                telemetry=telemetry_json,
                hash_algorithms=list(ctx.hash_algorithms),
                block_tags=tuple(ctx.block_tags),
                add_tags=tuple(ctx.add_tags),
                wandb_to_trackio=ctx.wandb_to_trackio,
            )

            # Register proxy artifacts first so downstream output/input queries include them.
            self._proxy_artifact_registrar.register(db_ctx, job_id, s3_entries)

            written_file_info = db_ctx.jobs.get_outputs(job_id)
            read_file_info = db_ctx.jobs.get_inputs(job_id)

            session = db_ctx.sessions.get_active()
            if session:
                stale_upstream, stale_downstream = self._staleness_analyzer.analyze(
                    db_ctx, session["id"], job_id
                )

            # Collect DAG stats and parent job lookups in the same DB context
            # to avoid opening a 2nd context in the coordinator.
            dag_stats = self._collect_dag_stats(db_ctx, session, job_uid, read_file_info)

        return (
            job_id,
            job_uid,
            read_file_info,
            written_file_info,
            stale_upstream,
            stale_downstream,
            dag_stats,
        )

    def _build_metadata_json(
        self,
        ctx: RunContext,
        prov: dict[str, Any],
        git_info: dict[str, Any],
        read_files: list[str],
        written_files: list[str],
        session_window_paths: list[str] | None = None,
        dataset_hint_paths: list[str] | None = None,
    ) -> str | None:
        metadata: dict[str, Any] = {}

        # Freeze the roar version that produced this job. Captured at run-record time so
        # the derived `roar.version` system label reflects the producer, not whatever roar
        # later re-derives the labels (e.g. on put). See system_labels.build_job_system_labels.
        metadata["roar_version"] = _ROAR_VERSION

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

        inference_paths = [*read_files, *written_files]
        if session_window_paths:
            inference_paths.extend(session_window_paths)
        if dataset_hint_paths:
            inference_paths.extend(dataset_hint_paths)
        if inference_paths:
            dataset_identifiers = self._dataset_identifier_inferer.infer(
                inference_paths, repo_root=ctx.repo_root
            )
            if dataset_identifiers:
                metadata["dataset_identifiers"] = dataset_identifiers

        # Include persistent env vars from [env] config section for reproduction.
        try:
            from ...core.models.run import resolve_run_config_start_dir
            from ...integrations.config import load_config

            config = load_config(start_dir=str(resolve_run_config_start_dir(ctx)))
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

    def _extract_dataset_hint_paths(self, command: list[str], repo_root: str) -> list[str]:
        """Extract likely dataset root paths from command-line flags."""
        hints: list[str] = []
        i = 0
        while i < len(command):
            token = command[i]
            if token.startswith("--"):
                if "=" in token:
                    flag, value = token.split("=", 1)
                    if flag in self._DATASET_HINT_FLAGS:
                        hints.extend(self._normalize_hint_value(value, repo_root))
                elif token in self._DATASET_HINT_FLAGS and i + 1 < len(command):
                    value = command[i + 1]
                    if not value.startswith("-"):
                        hints.extend(self._normalize_hint_value(value, repo_root))
                        i += 1
            i += 1

        # Preserve first-seen order
        return [p for p in dict.fromkeys(hints) if p]

    def _normalize_hint_value(self, value: str, repo_root: str) -> list[str]:
        values = [part for part in self._LIST_SEPARATORS.split(value.strip()) if part]
        normalized: list[str] = []
        for part in values:
            if "://" in part:
                normalized.append(part)
                continue

            path = Path(part)
            if not path.is_absolute():
                path = Path(repo_root) / path
            normalized.append(os.path.abspath(str(path)))
        return normalized

    @staticmethod
    def _collect_session_window_paths(
        db_ctx: Any,
        max_steps: int = 2,
        max_paths: int = 256,
    ) -> list[str]:
        """
        Collect recent session I/O paths to provide cross-step context for inference.

        This helps when a single step observes sparse container internals.
        """
        active = db_ctx.sessions.get_active()
        if not active:
            return []

        steps = db_ctx.sessions.get_steps(active["id"])
        numbered_steps = [step for step in steps if isinstance(step.get("step_number"), int)]
        if not numbered_steps:
            return []

        latest_step = max(step["step_number"] for step in numbered_steps)
        lower_bound = max(1, latest_step - max_steps)
        selected_job_ids = [
            step["id"] for step in numbered_steps if step["step_number"] >= lower_bound
        ]

        collected: list[str] = []
        for job_id in selected_job_ids:
            for io in db_ctx.jobs.get_inputs(job_id):
                path = io.get("path") or io.get("first_seen_path")
                if isinstance(path, str) and path:
                    collected.append(path)
                    if len(collected) >= max_paths:
                        return list(dict.fromkeys(collected))
            for io in db_ctx.jobs.get_outputs(job_id):
                path = io.get("path") or io.get("first_seen_path")
                if isinstance(path, str) and path:
                    collected.append(path)
                    if len(collected) >= max_paths:
                        return list(dict.fromkeys(collected))

        return list(dict.fromkeys(collected))

    @staticmethod
    def _collect_dag_stats(
        db_ctx: Any,
        session: dict | None,
        job_uid: str | None,
        read_file_info: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Collect DAG stats and parent job lookups within the existing DB context."""
        stats: dict[str, Any] = {
            "dag_jobs": 0,
            "dag_artifacts": 0,
            "dag_depth": 0,
            "step_number": None,
        }
        try:
            if session:
                from ...presenters.dag_data_builder import DagDataBuilder

                builder = DagDataBuilder(db_ctx, int(session["id"]))
                dag_data = builder.build(expanded=False)
                stats["dag_jobs"] = len(dag_data.get("nodes", []))
                stats["dag_artifacts"] = len(dag_data.get("artifacts", []))

                nodes = dag_data.get("nodes", [])
                if nodes:
                    step_deps = {n["step_number"]: n.get("dependencies", []) for n in nodes}
                    all_steps = set(step_deps)
                    memo: dict[int, int] = {}

                    def _depth(s: int) -> int:
                        if s in memo:
                            return memo[s]
                        children = [x for x in all_steps if s in step_deps.get(x, [])]
                        d = 1 + max((_depth(ch) for ch in children), default=0)
                        memo[s] = d
                        return d

                    roots = [s for s in all_steps if not step_deps.get(s)]
                    stats["dag_depth"] = max((_depth(r) for r in roots), default=1) if roots else 1

            for inp in read_file_info:
                aid = inp.get("artifact_id")
                if aid:
                    jobs_info = db_ctx.artifacts.get_jobs(aid)
                    producers = jobs_info.get("produced_by", [])
                    if producers:
                        inp["parent_job_uid"] = producers[0].get("job_uid")

            if job_uid:
                job_record = db_ctx.jobs.get_by_uid(job_uid)
                if job_record:
                    recorded_step = job_record.get("step_number")
                    if isinstance(recorded_step, int):
                        stats["step_number"] = recorded_step
        except Exception:
            pass

        return stats

    @staticmethod
    def _compute_cwd_relative(repo_root: str) -> str | None:
        try:
            relative = str(Path.cwd().relative_to(Path(repo_root)))
            return "" if relative == "." else relative
        except ValueError:
            return None
