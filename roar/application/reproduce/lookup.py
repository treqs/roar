"""Pipeline lookup helpers for reproduction workflows."""

from __future__ import annotations

from pathlib import Path

from ...core.interfaces.reproduction import PipelineInfo, PipelineLookupResult
from ...db.context import create_database_context
from ...integrations.glaas import GlaasClient
from ..lookup import lookup_remote_artifact, run_local_then_remote_lookup


def lookup_pipeline_result(
    *,
    hash_prefix: str,
    roar_dir: Path,
    server_url: str | None,
    glaas_client: GlaasClient | None,
) -> PipelineLookupResult:
    """Look up artifact pipeline information locally first, then via GLaaS."""
    lookup = run_local_then_remote_lookup(
        lookup_local=lambda: _lookup_local(hash_prefix, roar_dir),
        lookup_remote=lambda: _lookup_remote(
            hash_prefix=hash_prefix,
            server_url=server_url,
            glaas_client=glaas_client,
        ),
        allow_remote=bool(glaas_client or server_url),
    )
    if lookup.error:
        return PipelineLookupResult(pipeline=None, error=lookup.error, source=lookup.source.value)
    if lookup.value is not None:
        return PipelineLookupResult(
            pipeline=lookup.value,
            error=None,
            source=lookup.source.value,
        )

    return PipelineLookupResult(
        pipeline=None,
        error=(
            f"Artifact not found: {hash_prefix}\n"
            "If this artifact is on a remote server, check your authentication with 'roar auth test'."
        ),
        source=lookup.source.value,
    )


def _lookup_local(hash_prefix: str, roar_dir: Path) -> PipelineInfo | None:
    """Look up artifact and pipeline in the local database."""
    with create_database_context(roar_dir) as ctx:
        artifact = ctx.artifacts.get_by_hash(hash_prefix)
        if not artifact:
            return None

        artifact_hash = None
        for artifact_hash_row in artifact.get("hashes", []):
            if artifact_hash_row.get("algorithm") == "blake3":
                artifact_hash = artifact_hash_row.get("digest")
                break

        if not artifact_hash:
            return None

        jobs = ctx.artifacts.get_jobs(artifact["id"])
        producers = jobs.get("produced_by", [])
        if not producers:
            return None

        producer = producers[0]
        session_id = producer.get("session_id")
        if not session_id:
            return None

        session = ctx.sessions.get(session_id)
        if not session:
            return None

        steps = ctx.sessions.get_steps(session_id)
        build_steps: list[dict] = []
        run_steps: list[dict] = []

        for step in steps:
            step_dict = dict(step)
            step_dict["_inputs"] = ctx.jobs.get_inputs(step["id"])
            step_dict["_outputs"] = ctx.jobs.get_outputs(step["id"])
            if step.get("job_type") == "build":
                build_steps.append(step_dict)
            else:
                run_steps.append(step_dict)

        return PipelineInfo(
            artifact_hash=artifact_hash,
            git_repo=session.get("git_repo"),
            git_commit=session.get("git_commit_start") or session.get("git_commit_end"),
            build_steps=build_steps,
            run_steps=run_steps,
            total_steps=len(build_steps) + len(run_steps),
        )


def _lookup_remote(
    *,
    hash_prefix: str,
    server_url: str | None,
    glaas_client: GlaasClient | None,
) -> tuple[PipelineInfo | None, str | None]:
    """Look up artifact and pipeline from GLaaS."""
    client = glaas_client or GlaasClient(server_url)
    if not client:
        return None, "No GLaaS server configured"

    artifact, artifact_error = lookup_remote_artifact(
        hash_prefix=hash_prefix,
        artifact_reader=client,
        server_url=server_url,
    )
    if artifact_error:
        return None, artifact_error
    if not artifact:
        return None, None

    canonical_hash = str(artifact.get("hash") or hash_prefix)
    pipeline_data, error = client.get_artifact_dag(canonical_hash)
    if error:
        return None, error
    if not pipeline_data:
        return None, None

    build_steps = []
    run_steps = []
    for job in pipeline_data.get("jobs", []):
        if job.get("jobType") == "build":
            build_steps.append(job)
        else:
            run_steps.append(job)

    return (
        PipelineInfo(
            artifact_hash=canonical_hash,
            git_repo=pipeline_data.get("gitRepo"),
            git_commit=pipeline_data.get("gitCommit"),
            build_steps=build_steps,
            run_steps=run_steps,
            total_steps=len(build_steps) + len(run_steps),
        ),
        None,
    )
