"""Pipeline lookup helpers for reproduction workflows."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from ...core.interfaces.reproduction import PipelineInfo, PipelineLookupResult
from ...db.context import create_database_context
from ...integrations.glaas import GlaasClient
from ...publish_auth import load_publish_auth_context, resolve_publish_creator_identity
from ..lookup import lookup_remote_artifact, run_local_then_remote_lookup
from ..publish.lineage import LineageCollector
from ..publish.session import compute_canonical_lineage_session_hash

ReproduceTargetKind = Literal["artifact", "lineage"]


def lookup_pipeline_result(
    *,
    hash_prefix: str,
    roar_dir: Path,
    server_url: str | None,
    glaas_client: GlaasClient | None,
    target_kind: ReproduceTargetKind = "artifact",
) -> PipelineLookupResult:
    """Look up pipeline information locally first, then via GLaaS."""
    lookup = run_local_then_remote_lookup(
        lookup_local=lambda: _lookup_local(hash_prefix, roar_dir, target_kind=target_kind),
        lookup_remote=lambda: _lookup_remote(
            hash_prefix=hash_prefix,
            server_url=server_url,
            glaas_client=glaas_client,
            target_kind=target_kind,
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

    target_label = "Lineage" if target_kind == "lineage" else "Artifact"
    return PipelineLookupResult(
        pipeline=None,
        error=(
            f"{target_label} not found: {hash_prefix}\n"
            f"If this {target_label.lower()} is on a remote server, check your authentication with 'roar auth test'."
        ),
        source=lookup.source.value,
    )


def _lookup_local(
    hash_prefix: str,
    roar_dir: Path,
    *,
    target_kind: ReproduceTargetKind,
) -> PipelineInfo | None:
    """Look up a local pipeline for artifact or lineage reproduction."""
    if target_kind == "lineage":
        return _lookup_local_lineage(hash_prefix, roar_dir)
    return _lookup_local_artifact(hash_prefix, roar_dir)


def _lookup_remote(
    *,
    hash_prefix: str,
    server_url: str | None,
    glaas_client: GlaasClient | None,
    target_kind: ReproduceTargetKind,
) -> tuple[PipelineInfo | None, str | None]:
    """Look up a remote pipeline for artifact or lineage reproduction."""
    if target_kind == "lineage":
        return _lookup_remote_lineage(
            hash_prefix=hash_prefix,
            server_url=server_url,
            glaas_client=glaas_client,
        )
    return _lookup_remote_artifact_pipeline(
        hash_prefix=hash_prefix,
        server_url=server_url,
        glaas_client=glaas_client,
    )


def _lookup_local_artifact(hash_prefix: str, roar_dir: Path) -> PipelineInfo | None:
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

        return _build_local_session_pipeline(
            ctx=ctx,
            session=session,
            session_id=int(session_id),
            artifact_hash=artifact_hash,
            target_kind="artifact",
            session_hash=None,
        )


def _lookup_local_lineage(hash_prefix: str, roar_dir: Path) -> PipelineInfo | None:
    """Look up a lineage/session pipeline in the local database by full canonical hash."""
    lineage_collector = LineageCollector()
    creator_identity = resolve_publish_creator_identity(
        load_publish_auth_context(roar_dir.parent, allow_public_without_binding=True)
    )

    with create_database_context(roar_dir) as ctx:
        for session in ctx.sessions.get_all():
            session_id = int(session["id"])
            lineage = lineage_collector.collect_session(session_id, roar_dir)
            if not getattr(lineage, "jobs", None):
                continue

            canonical_hash = compute_canonical_lineage_session_hash(
                lineage=lineage,
                creator_identity=creator_identity,
            )
            if canonical_hash != hash_prefix:
                continue

            return _build_local_session_pipeline(
                ctx=ctx,
                session=session,
                session_id=session_id,
                artifact_hash="",
                target_kind="lineage",
                session_hash=canonical_hash,
            )

    return None


def _build_local_session_pipeline(
    *,
    ctx,
    session: dict,
    session_id: int,
    artifact_hash: str,
    target_kind: ReproduceTargetKind,
    session_hash: str | None,
) -> PipelineInfo:
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
        target_kind=target_kind,
        session_hash=session_hash,
        build_steps=build_steps,
        run_steps=run_steps,
        total_steps=len(build_steps) + len(run_steps),
    )


def _lookup_remote_artifact_pipeline(
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

    build_steps, run_steps = _split_remote_jobs(pipeline_data.get("jobs", []))

    return (
        PipelineInfo(
            artifact_hash=canonical_hash,
            git_repo=pipeline_data.get("gitRepo"),
            git_commit=pipeline_data.get("gitCommit"),
            target_kind="artifact",
            session_hash=None,
            build_steps=build_steps,
            run_steps=run_steps,
            total_steps=len(build_steps) + len(run_steps),
        ),
        None,
    )


def _lookup_remote_lineage(
    *,
    hash_prefix: str,
    server_url: str | None,
    glaas_client: GlaasClient | None,
) -> tuple[PipelineInfo | None, str | None]:
    """Look up a lineage/session replay payload from GLaaS."""
    client = glaas_client or GlaasClient(server_url)
    if not client:
        return None, "No GLaaS server configured"

    pipeline_data, error = client.get_session_reproduction(hash_prefix)
    if error:
        if error.startswith("HTTP 404:"):
            return None, None
        return None, error
    if not pipeline_data:
        return None, None

    build_steps, run_steps = _split_remote_jobs(pipeline_data.get("jobs", []))

    return (
        PipelineInfo(
            artifact_hash="",
            git_repo=pipeline_data.get("gitRepo"),
            git_commit=pipeline_data.get("gitCommit"),
            target_kind="lineage",
            session_hash=str(pipeline_data.get("sessionHash") or hash_prefix),
            build_steps=build_steps,
            run_steps=run_steps,
            total_steps=len(build_steps) + len(run_steps),
        ),
        None,
    )


def _split_remote_jobs(jobs: list[dict]) -> tuple[list[dict], list[dict]]:
    build_steps: list[dict] = []
    run_steps: list[dict] = []
    for job in jobs:
        if job.get("jobType") == "build":
            build_steps.append(job)
        else:
            run_steps.append(job)
    return build_steps, run_steps
