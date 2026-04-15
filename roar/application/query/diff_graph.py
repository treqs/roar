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

    next_remote_job_id = -1
    for job_data in payload.get("jobs", []):
        next_remote_job_id = append_glaas_job_nodes(job_data, job_nodes, next_remote_job_id)

    job_nodes.sort(key=lambda job: (job.step_number or 0, job.job_id))

    return LineageGraph(
        target_artifact_id=f"glaas:{ref_key}",
        target_hash=target_hash,
        target_path=target_path,
        jobs=job_nodes,
    )


def append_glaas_job_nodes(
    job_data: Any,
    job_nodes: list[JobNode],
    next_remote_job_id: int,
) -> int:
    """Append one GLaaS job and any nested children to the normalized graph."""
    if not isinstance(job_data, dict):
        return next_remote_job_id

    fallback_job_id = next_remote_job_id
    input_hashes, input_paths = collect_glaas_artifact_info(job_data.get("inputs", []))
    output_hashes, output_paths = collect_glaas_artifact_info(job_data.get("outputs", []))

    job_nodes.append(
        JobNode(
            job_id=coerce_glaas_job_id(job_data.get("id"), fallback_job_id),
            job_uid=str(
                first_present(job_data, "jobUid", "job_uid") or f"remote-{abs(fallback_job_id)}"
            ),
            command=str(job_data.get("command") or ""),
            step_number=coerce_int(first_present(job_data, "stepNumber", "step_number")),
            git_commit=coerce_optional_str(first_present(job_data, "gitCommit", "git_commit")),
            git_branch=coerce_optional_str(first_present(job_data, "gitBranch", "git_branch")),
            metadata=parse_metadata(job_data.get("metadata")),
            input_hashes=input_hashes,
            output_hashes=output_hashes,
            input_paths=input_paths,
            output_paths=output_paths,
        )
    )
    next_remote_job_id -= 1

    children = job_data.get("children", [])
    if isinstance(children, list):
        for child in children:
            next_remote_job_id = append_glaas_job_nodes(child, job_nodes, next_remote_job_id)

    return next_remote_job_id


def collect_glaas_artifact_info(entries: Any) -> tuple[dict[str, str], dict[str, str]]:
    """Normalize GLaaS job I/O entries into hash/path maps keyed by digest."""
    hashes: dict[str, str] = {}
    paths: dict[str, str] = {}
    if not isinstance(entries, list):
        return hashes, paths

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        artifact_hash = entry.get("hash") or entry.get("artifact_hash")
        if not artifact_hash:
            continue
        artifact_key = str(artifact_hash)
        hashes[artifact_key] = artifact_key
        paths[artifact_key] = str(entry.get("path") or entry.get("first_seen_path") or "")

    return hashes, paths


def first_present(mapping: dict[str, Any], *keys: str) -> Any:
    """Return the first non-None field from a mapping."""
    for key in keys:
        value = mapping.get(key)
        if value is not None:
            return value
    return None


def coerce_glaas_job_id(raw_job_id: Any, fallback_job_id: int) -> int:
    """Coerce a remote job id into an integer, falling back to a synthetic id."""
    if isinstance(raw_job_id, bool):
        return fallback_job_id
    if isinstance(raw_job_id, int):
        return raw_job_id
    if isinstance(raw_job_id, str):
        with suppress(ValueError):
            return int(raw_job_id)
    return fallback_job_id


def coerce_int(value: Any) -> int | None:
    """Coerce a value to ``int`` when possible."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        with suppress(ValueError):
            return int(value)
    return None


def coerce_optional_str(value: Any) -> str | None:
    """Return ``str(value)`` for present values, otherwise ``None``."""
    if value is None:
        return None
    return str(value)


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
