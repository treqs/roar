"""Application orchestration for the local lineage query."""

from __future__ import annotations

import json
import os

from ...db.context import create_database_context
from ...db.hashing.backend import compute_hashes_batch
from .requests import LineageQueryRequest
from .results import LineageArtifactSummary, LineageJobSummary, LineageSummary


def render_lineage(request: LineageQueryRequest) -> str:
    """Render artifact lineage as JSON."""
    return json.dumps(build_lineage_summary(request).to_dict(), indent=2)


def build_lineage_summary(request: LineageQueryRequest) -> LineageSummary:
    """Build a typed lineage summary for an artifact."""
    with create_database_context(request.roar_dir) as db_ctx:
        artifact_hash = None
        file_path = None

        if "/" in request.artifact or os.path.exists(request.artifact):
            file_path = request.artifact
            artifact_hash = _resolve_artifact_hash(request.artifact, request.cwd)
            if not artifact_hash:
                raise ValueError(f"File not found: {request.artifact}")
        else:
            artifact_hash = request.artifact

        db_artifact = db_ctx.artifacts.get_by_hash(artifact_hash, algorithm="blake3")
        if not db_artifact:
            db_artifact = db_ctx.artifacts.get_by_hash(artifact_hash)

        if not db_artifact:
            raise ValueError(
                f"Artifact not found in database: {request.artifact}\n"
                "The file may not have been tracked by roar yet."
            )

        target_artifact, jobs, _on_path_hashes = db_ctx.lineage.get_filtered_lineage(
            db_artifact["id"], max_depth=request.depth
        )
        if not target_artifact:
            raise ValueError(f"Could not trace lineage for artifact: {request.artifact}")

    target_hash = None
    for digest in target_artifact.get("hashes", []):
        if digest.get("algorithm") == "blake3":
            target_hash = digest.get("digest")
            break
    if not target_hash:
        raise ValueError("Artifact has no BLAKE3 hash")

    target_path = file_path or target_artifact.get("first_seen_path", "")
    jobs_list = [
        LineageJobSummary(
            job_uid=str(job.get("job_uid", "")),
            step_number=job.get("step_number"),
            command=str(job.get("command", "")),
            timestamp=job.get("timestamp", 0),
            duration_seconds=job.get("duration_seconds"),
            exit_code=job.get("exit_code"),
            inputs=list(job.get("_inputs", [])),
            outputs=list(job.get("_outputs", [])),
        )
        for job in jobs
    ]

    seen_hashes: set[str] = set()
    artifacts_list: list[LineageArtifactSummary] = []
    for job in jobs:
        for input_artifact in job.get("_inputs", []):
            if input_artifact["hash"] not in seen_hashes:
                seen_hashes.add(input_artifact["hash"])
                artifacts_list.append(
                    LineageArtifactSummary(
                        hash=str(input_artifact["hash"]),
                        path=str(input_artifact["path"]),
                        size=int(input_artifact.get("size", 0)),
                    )
                )
        for output_artifact in job.get("_outputs", []):
            if output_artifact["hash"] not in seen_hashes:
                seen_hashes.add(output_artifact["hash"])
                artifacts_list.append(
                    LineageArtifactSummary(
                        hash=str(output_artifact["hash"]),
                        path=str(output_artifact["path"]),
                        size=int(output_artifact.get("size", 0)),
                    )
                )

    if target_hash not in seen_hashes:
        artifacts_list.append(
            LineageArtifactSummary(
                hash=target_hash,
                path=target_path,
                size=int(target_artifact.get("size", 0)),
            )
        )

    return LineageSummary(
        artifact=LineageArtifactSummary(
            hash=target_hash,
            path=target_path,
            size=int(target_artifact.get("size", 0)),
        ),
        jobs=jobs_list,
        artifacts=artifacts_list,
    )


def _resolve_artifact_hash(path: str, cwd) -> str | None:
    if not os.path.isabs(path):
        path = str(cwd / path)
    if not os.path.exists(path):
        return None
    return compute_hashes_batch([path], ["blake3"]).get(path, {}).get("blake3")
