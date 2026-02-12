"""
Distributed Ray lineage ingestion service.

Converts captured Ray task events into local roar jobs/artifacts so the
existing DAG, lineage, and registration flows can treat distributed runs the
same way they treat local traced runs.
"""

from __future__ import annotations

import json
import shlex
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ...db.context import create_database_context
from ...db.hashing.backend import compute_hashes
from ...ray import ref_hashes
from .job_recording import ProxyArtifactRegistrar, StalenessAnalyzer

if TYPE_CHECKING:
    from ...core.interfaces.run import RunContext
    from .proxy import S3LogEntry


@dataclass(frozen=True)
class _ArtifactRecord:
    path: str
    hashes: dict[str, str]
    size: int = 0
    source_type: str | None = None
    source_url: str | None = None


class RayLineageRecorder:
    """Persist Ray task events as roar jobs/artifacts."""

    def __init__(
        self,
        staleness_analyzer: StalenessAnalyzer | None = None,
        proxy_artifact_registrar: ProxyArtifactRegistrar | None = None,
    ) -> None:
        self._staleness_analyzer = staleness_analyzer or StalenessAnalyzer()
        self._proxy_artifact_registrar = proxy_artifact_registrar or ProxyArtifactRegistrar()

    def record(
        self,
        ctx: RunContext,
        events: list[dict[str, Any]],
        execution_result: Any,
        start_time: float,
        s3_entries: list[S3LogEntry] | None = None,
        run_id: str | None = None,
    ) -> tuple[int, str, list[dict[str, Any]], list[dict[str, Any]], list[int], list[int]]:
        """
        Persist distributed task events and return payload expected by RunCoordinator.

        Returns:
            (job_id, job_uid, inputs, outputs, stale_upstream, stale_downstream)
        """
        sorted_events = sorted(events, key=self._event_sort_key)
        fallback_timestamp = self._safe_float(start_time, default=time.time())
        last_job_id = 0
        last_job_uid = "000000"
        stale_upstream: list[int] = []
        stale_downstream: list[int] = []

        with create_database_context(ctx.roar_dir) as db_ctx:
            session_id = db_ctx.sessions.get_or_create_active()
            if ctx.git_commit:
                db_ctx.sessions.update_git_commits(session_id, ctx.git_commit, update_start=True)

            for index, event in enumerate(sorted_events):
                task_name = self._task_name(event, index)
                command = f"ray::{task_name}"
                timestamp = self._safe_float(
                    event.get("start_time"), default=fallback_timestamp + (index * 1e-6)
                )
                duration = self._safe_duration(event, execution_result)
                exit_code = self._safe_int(event.get("exit_code"), default=0)

                input_records = self._collect_inputs(event)
                output_records = self._collect_outputs(event)
                input_paths = [record.path for record in input_records]
                output_paths = [record.path for record in output_records]

                step_identity = db_ctx.session_service.compute_step_identity(
                    input_paths,
                    output_paths,
                    repo_root=ctx.repo_root,
                    command=command,
                )
                existing_step = db_ctx.sessions.get_step_by_identity(session_id, step_identity)
                if existing_step:
                    step_number = existing_step["step_number"]
                else:
                    step_number = db_ctx.sessions.get_next_step_number(session_id)

                db_ctx.sessions.update_current_step(session_id, step_number)
                metadata = self._build_job_metadata(
                    event=event,
                    run_id=run_id,
                    task_name=task_name,
                    input_count=len(input_records),
                    output_count=len(output_records),
                )
                job_id, job_uid = db_ctx.jobs.create(
                    command=command,
                    timestamp=timestamp,
                    step_identity=step_identity,
                    session_id=session_id,
                    step_number=step_number,
                    step_name=task_name,
                    git_repo=ctx.git_repo,
                    git_commit=ctx.git_commit,
                    git_branch=ctx.git_branch,
                    duration_seconds=duration,
                    exit_code=exit_code,
                    metadata=metadata,
                    job_type=ctx.job_type,
                )

                self._attach_artifacts(db_ctx, job_id, input_records, is_input=True)
                self._attach_artifacts(db_ctx, job_id, output_records, is_input=False)

                last_job_id = job_id
                last_job_uid = job_uid

            if last_job_id == 0:
                last_job_id, last_job_uid = self._record_fallback_job(
                    db_ctx=db_ctx,
                    ctx=ctx,
                    session_id=session_id,
                    timestamp=fallback_timestamp,
                    execution_result=execution_result,
                    run_id=run_id,
                    event_count=len(sorted_events),
                )

            self._proxy_artifact_registrar.register(db_ctx, last_job_id, s3_entries)

            inputs = db_ctx.jobs.get_inputs(last_job_id)
            outputs = db_ctx.jobs.get_outputs(last_job_id)

            session = db_ctx.sessions.get_active()
            if session:
                stale_upstream, stale_downstream = self._staleness_analyzer.analyze(
                    db_ctx, session["id"], last_job_id
                )

        return last_job_id, last_job_uid, inputs, outputs, stale_upstream, stale_downstream

    def _collect_inputs(self, event: dict[str, Any]) -> list[_ArtifactRecord]:
        records: list[_ArtifactRecord] = []
        seen: set[tuple[str, str]] = set()

        for desc in event.get("inputs", []):
            record = self._descriptor_to_artifact(desc)
            if record is None:
                continue
            key = (record.path, record.hashes.get("blake3", ""))
            if key not in seen:
                records.append(record)
                seen.add(key)

        for ref in event.get("input_refs", []):
            record = self._ref_to_artifact(ref)
            key = (record.path, record.hashes.get("blake3", ""))
            if key not in seen:
                records.append(record)
                seen.add(key)

        return records

    def _collect_outputs(self, event: dict[str, Any]) -> list[_ArtifactRecord]:
        records: list[_ArtifactRecord] = []
        seen: set[tuple[str, str]] = set()

        for desc in event.get("outputs", []):
            record = self._descriptor_to_artifact(desc)
            if record is None:
                continue
            key = (record.path, record.hashes.get("blake3", ""))
            if key not in seen:
                records.append(record)
                seen.add(key)

        for ref in event.get("output_refs", []):
            record = self._ref_to_artifact(ref)
            key = (record.path, record.hashes.get("blake3", ""))
            if key not in seen:
                records.append(record)
                seen.add(key)

        return records

    def _descriptor_to_artifact(self, descriptor: Any) -> _ArtifactRecord | None:
        if not isinstance(descriptor, dict):
            return None
        path = str(descriptor.get("path") or "").strip()
        if not path:
            return None

        hashes = self._normalize_hash_map(descriptor.get("hashes"))
        if "blake3" not in hashes:
            computed = compute_hashes(path, ["blake3", "sha256"])
            if computed:
                hashes.update(computed)
        if "blake3" not in hashes:
            return None

        size = self._safe_int(descriptor.get("size"), default=0)
        source_type = descriptor.get("source_type")
        source_url = descriptor.get("source_url")
        return _ArtifactRecord(
            path=path,
            hashes=hashes,
            size=size,
            source_type=source_type if isinstance(source_type, str) else None,
            source_url=source_url if isinstance(source_url, str) else None,
        )

    def _ref_to_artifact(self, ref: Any) -> _ArtifactRecord:
        ref_value = str(ref).strip()
        hashes = ref_hashes(ref_value)
        path = f"ray://object/{ref_value}"
        return _ArtifactRecord(
            path=path,
            hashes=hashes,
            size=0,
            source_type="ray",
            source_url=path,
        )

    @staticmethod
    def _normalize_hash_map(value: Any) -> dict[str, str]:
        if not isinstance(value, dict):
            return {}
        normalized: dict[str, str] = {}
        for algorithm, digest in value.items():
            if not isinstance(algorithm, str) or not isinstance(digest, str):
                continue
            if not digest:
                continue
            normalized[algorithm] = digest.lower()
        return normalized

    @staticmethod
    def _event_sort_key(event: dict[str, Any]) -> tuple[float, float, str]:
        start = RayLineageRecorder._safe_float(event.get("start_time"), default=0.0)
        end = RayLineageRecorder._safe_float(event.get("end_time"), default=start)
        name = str(event.get("task_name") or "")
        return start, end, name

    @staticmethod
    def _task_name(event: dict[str, Any], index: int) -> str:
        task_name = str(event.get("task_name") or "").strip()
        if task_name:
            return task_name
        return f"task_{index + 1}"

    @staticmethod
    def _safe_float(value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _safe_int(value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _safe_duration(self, event: dict[str, Any], execution_result: Any) -> float | None:
        if "duration_seconds" in event:
            return max(0.0, self._safe_float(event.get("duration_seconds"), default=0.0))
        start = self._safe_float(event.get("start_time"), default=0.0)
        end = self._safe_float(event.get("end_time"), default=start)
        if start > 0 and end >= start:
            return end - start

        try:
            return max(0.0, float(execution_result.duration))
        except Exception:
            return None

    @staticmethod
    def _build_job_metadata(
        event: dict[str, Any],
        run_id: str | None,
        task_name: str,
        input_count: int,
        output_count: int,
    ) -> str:
        metadata = {
            "execution_backend": "ray",
            "run_id": run_id,
            "task_name": task_name,
            "task_id": event.get("task_id"),
            "attempt": event.get("attempt"),
            "actor_id": event.get("actor_id"),
            "node_id": event.get("node_id"),
            "job_id": event.get("job_id"),
            "input_count": input_count,
            "output_count": output_count,
            "error": event.get("error"),
            "metadata": event.get("metadata") if isinstance(event.get("metadata"), dict) else None,
        }
        return json.dumps(metadata)

    def _attach_artifacts(
        self,
        db_ctx: Any,
        job_id: int,
        records: list[_ArtifactRecord],
        *,
        is_input: bool,
    ) -> None:
        for record in records:
            artifact_id, _created = db_ctx.artifacts.register(
                hashes=record.hashes,
                size=record.size,
                path=record.path,
                source_type=record.source_type,
                source_url=record.source_url,
            )
            if is_input:
                db_ctx.jobs.add_input(job_id, artifact_id, record.path)
            else:
                db_ctx.jobs.add_output(job_id, artifact_id, record.path)

    def _record_fallback_job(
        self,
        db_ctx: Any,
        ctx: RunContext,
        session_id: int,
        timestamp: float,
        execution_result: Any,
        run_id: str | None,
        event_count: int,
    ) -> tuple[int, str]:
        command = shlex.join(ctx.command)
        step_identity = db_ctx.session_service.compute_step_identity(
            [],
            [],
            repo_root=ctx.repo_root,
            command=command,
        )
        existing_step = db_ctx.sessions.get_step_by_identity(session_id, step_identity)
        if existing_step:
            step_number = existing_step["step_number"]
        else:
            step_number = db_ctx.sessions.get_next_step_number(session_id)
        db_ctx.sessions.update_current_step(session_id, step_number)

        try:
            duration = max(0.0, float(execution_result.duration))
        except Exception:
            duration = None
        try:
            exit_code = int(execution_result.exit_code)
        except Exception:
            exit_code = 1

        metadata = json.dumps(
            {
                "execution_backend": "ray",
                "run_id": run_id,
                "lineage_events": event_count,
                "note": "no distributed task lineage events were captured",
            }
        )
        return db_ctx.jobs.create(
            command=command,
            timestamp=timestamp,
            step_identity=step_identity,
            session_id=session_id,
            step_number=step_number,
            step_name=ctx.step_name,
            git_repo=ctx.git_repo,
            git_commit=ctx.git_commit,
            git_branch=ctx.git_branch,
            duration_seconds=duration,
            exit_code=exit_code,
            metadata=metadata,
            job_type=ctx.job_type,
        )
