"""Lineage graph models and graph-building helpers for `roar diff`."""

from __future__ import annotations

import json
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...core.digests import extract_primary_digest
from ...db.query_context import QueryDatabaseContext
from .diff_refs import (
    DiffError,
    classify_diff_ref,
    glaas_ref_key,
    resolve_ref_to_artifact_id,
    resolve_session_ref,
)


@dataclass(frozen=True)
class JobNode:
    """A normalized job node in a lineage graph."""

    job_id: int
    job_uid: str
    command: str
    step_number: int | None
    git_commit: str | None
    git_branch: str | None
    metadata: dict[str, Any] | None
    input_hashes: dict[str, str]
    output_hashes: dict[str, str]
    input_paths: dict[str, str]
    output_paths: dict[str, str]


@dataclass(frozen=True)
class LineageGraph:
    """A normalized lineage graph for one diff target."""

    target_artifact_id: str
    target_hash: str | None
    target_path: str | None
    jobs: list[JobNode]


@dataclass(frozen=True)
class JobMatch:
    """A matched pair of jobs, one from each lineage graph."""

    job_a: JobNode
    job_b: JobNode


def build_diff_graph(
    db_ctx: QueryDatabaseContext | None,
    ref: str,
    cwd: Path,
    max_depth: int,
) -> LineageGraph:
    """Resolve any supported diff reference into a normalized lineage graph."""
    ref_type = classify_diff_ref(ref)

    if ref_type == "glaas":
        return build_glaas_graph(ref)

    if ref_type == "session":
        if db_ctx is None:
            raise DiffError("Local database required for session refs.")
        session = resolve_session_ref(db_ctx, ref)
        return build_session_lineage_graph(db_ctx, session)

    if db_ctx is None:
        raise DiffError("Local database required for local refs.")
    artifact_id, display_path = resolve_ref_to_artifact_id(db_ctx, ref, cwd)
    return build_local_lineage_graph(db_ctx, artifact_id, display_path, max_depth)


def build_session_lineage_graph(
    db_ctx: QueryDatabaseContext,
    session: dict[str, Any],
) -> LineageGraph:
    """Build a lineage graph from all jobs in a local session."""
    steps = db_ctx.sessions.get_steps(int(session["id"]))
    job_nodes: list[JobNode] = []

    for step in steps:
        job_id = step["id"]
        inputs = db_ctx.jobs.get_inputs(job_id)
        outputs = db_ctx.jobs.get_outputs(job_id)

        input_hashes, input_paths = collect_artifact_info(inputs)
        output_hashes, output_paths = collect_artifact_info(outputs)
        metadata = parse_metadata(step.get("metadata"))

        job_nodes.append(
            JobNode(
                job_id=job_id,
                job_uid=step.get("job_uid", ""),
                command=step.get("command", ""),
                step_number=step.get("step_number"),
                git_commit=step.get("git_commit"),
                git_branch=step.get("git_branch"),
                metadata=metadata,
                input_hashes=input_hashes,
                output_hashes=output_hashes,
                input_paths=input_paths,
                output_paths=output_paths,
            )
        )

    job_nodes.sort(key=lambda job: (job.step_number or 0, job.job_id))

    return LineageGraph(
        target_artifact_id=f"session:{session['hash'] or session['id']}",
        target_hash=session.get("hash"),
        target_path=None,
        jobs=job_nodes,
    )


def build_glaas_graph(ref: str) -> LineageGraph:
    """Fetch and normalize a remote GLaaS DAG/lineage payload."""
    ref_key = glaas_ref_key(ref)

    try:
        from ...integrations.glaas.client import GlaasClient
    except ImportError as exc:
        raise DiffError("GLaaS client not available.") from exc

    client = GlaasClient()
    if not client.is_configured():
        raise DiffError("GLaaS URL not configured. Run 'roar config set glaas.url <url>'.")

    result, error = client.get_artifact_dag(ref_key)
    if error:
        result, error = client.get_artifact_lineage(ref_key, depth=10)
        if error:
            raise DiffError(f"GLaaS lookup failed for '{ref_key}': {error}")

    return glaas_payload_to_graph(ref_key, result)


def glaas_payload_to_graph(ref_key: str, payload: dict[str, Any] | None) -> LineageGraph:
    """Convert a GLaaS API response into a normalized lineage graph."""
    if not payload:
        raise DiffError(f"Empty response from GLaaS for '{ref_key}'.")

    job_nodes: list[JobNode] = []
    target_hash: str | None = None
    target_path: str | None = None

    artifact = payload.get("artifact")
    if isinstance(artifact, dict):
        hashes = artifact.get("hashes", [])
        for hash_entry in hashes:
            if isinstance(hash_entry, dict) and hash_entry.get("algorithm") == "blake3":
                target_hash = hash_entry["digest"]
                break
        if not target_hash and hashes:
            target_hash = hashes[0].get("digest") if isinstance(hashes[0], dict) else None
        target_path = artifact.get("first_seen_path") or artifact.get("path")

    jobs_list = payload.get("jobs", [])
    for i, job_data in enumerate(jobs_list):
        if not isinstance(job_data, dict):
            continue

        input_hashes: dict[str, str] = {}
        input_paths: dict[str, str] = {}
        for inp in job_data.get("inputs", []):
            if isinstance(inp, dict):
                artifact_hash = inp.get("hash", inp.get("artifact_hash", ""))
                path = inp.get("path", "")
                if artifact_hash:
                    input_hashes[artifact_hash] = artifact_hash
                    input_paths[artifact_hash] = path

        output_hashes: dict[str, str] = {}
        output_paths: dict[str, str] = {}
        for out in job_data.get("outputs", []):
            if isinstance(out, dict):
                artifact_hash = out.get("hash", out.get("artifact_hash", ""))
                path = out.get("path", "")
                if artifact_hash:
                    output_hashes[artifact_hash] = artifact_hash
                    output_paths[artifact_hash] = path

        metadata = None
        raw_meta = job_data.get("metadata")
        if isinstance(raw_meta, str):
            with suppress(json.JSONDecodeError):
                metadata = json.loads(raw_meta)
        elif isinstance(raw_meta, dict):
            metadata = raw_meta

        job_nodes.append(
            JobNode(
                job_id=-(i + 1),
                job_uid=job_data.get("job_uid", f"remote-{i}"),
                command=job_data.get("command", ""),
                step_number=job_data.get("step_number"),
                git_commit=job_data.get("git_commit"),
                git_branch=job_data.get("git_branch"),
                metadata=metadata,
                input_hashes=input_hashes,
                output_hashes=output_hashes,
                input_paths=input_paths,
                output_paths=output_paths,
            )
        )

    job_nodes.sort(key=lambda job: (job.step_number or 0, job.job_id))

    return LineageGraph(
        target_artifact_id=f"glaas:{ref_key}",
        target_hash=target_hash,
        target_path=target_path,
        jobs=job_nodes,
    )


def build_local_lineage_graph(
    db_ctx: QueryDatabaseContext,
    artifact_id: str,
    display_path: str | None,
    max_depth: int,
) -> LineageGraph:
    """Extract the upstream local lineage graph for an artifact."""
    artifact = db_ctx.artifacts.get(artifact_id)
    target_hash = get_primary_hash(artifact) if artifact else None

    visited_jobs: set[int] = set()
    visited_artifacts: set[str] = set()
    job_nodes: list[JobNode] = []

    def trace_upstream(art_id: str, depth: int) -> None:
        if depth > max_depth or art_id in visited_artifacts:
            return
        visited_artifacts.add(art_id)

        jobs_info = db_ctx.artifacts.get_jobs(art_id)
        produced_by = jobs_info.get("produced_by", [])
        producer = produced_by[0] if produced_by else None

        if producer and producer["id"] not in visited_jobs:
            visited_jobs.add(producer["id"])
            job_id = producer["id"]

            inputs = db_ctx.jobs.get_inputs(job_id)
            outputs = db_ctx.jobs.get_outputs(job_id)

            input_hashes, input_paths = collect_artifact_info(inputs)
            output_hashes, output_paths = collect_artifact_info(outputs)
            metadata = parse_metadata(producer.get("metadata"))

            job_nodes.append(
                JobNode(
                    job_id=job_id,
                    job_uid=producer.get("job_uid", ""),
                    command=producer.get("command", ""),
                    step_number=producer.get("step_number"),
                    git_commit=producer.get("git_commit"),
                    git_branch=producer.get("git_branch"),
                    metadata=metadata,
                    input_hashes=input_hashes,
                    output_hashes=output_hashes,
                    input_paths=input_paths,
                    output_paths=output_paths,
                )
            )

            for inp in inputs:
                trace_upstream(inp["artifact_id"], depth + 1)

    trace_upstream(artifact_id, 0)
    job_nodes.sort(key=lambda job: (job.step_number or 0, job.job_id))

    return LineageGraph(
        target_artifact_id=artifact_id,
        target_hash=target_hash,
        target_path=display_path,
        jobs=job_nodes,
    )


def collect_artifact_info(
    artifacts: list[dict[str, Any]],
) -> tuple[dict[str, str], dict[str, str]]:
    """Build ``artifact_id -> hash/path`` maps for a job I/O list."""
    hashes: dict[str, str] = {}
    paths: dict[str, str] = {}
    for artifact in artifacts:
        artifact_id = str(artifact["artifact_id"])
        digest = get_primary_hash(artifact)
        if digest:
            hashes[artifact_id] = digest
        paths[artifact_id] = artifact.get("path") or artifact.get("first_seen_path") or ""
    return hashes, paths


def parse_metadata(raw: Any) -> dict[str, Any] | None:
    """Parse metadata from string or dict payloads."""
    if not raw:
        return None
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None
    if isinstance(raw, dict):
        return raw
    return None


def get_primary_hash(item: dict[str, Any] | None) -> str | None:
    """Extract the preferred primary digest from a local artifact row."""
    return extract_primary_digest(item)
