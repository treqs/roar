"""Shared publish-session orchestration helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ...core.canonical_session import compute_canonical_session_hash
from ...core.interfaces.lineage import LineageData
from ...core.interfaces.logger import ILogger
from ...core.interfaces.registration import GitContext, SessionRegistrationResult
from .remote_registry import RemoteRegistryTransport, coerce_remote_registry


class PublishSessionService(Protocol):
    """Minimal session-service contract for publish workflows."""

    def compute_session_hash(self, *, roar_dir: str, session_id: int | None) -> str:
        """Compute the local session hash."""

    def register(
        self,
        session_hash: str,
        git_context: GitContext,
    ) -> SessionRegistrationResult:
        """Register a session with GLaaS."""

    def create_registration_session(
        self,
        client_session_id: str | None = None,
        mode: str | None = None,
    ) -> SessionRegistrationResult:
        """Create or resume a remote registration session."""


@dataclass(frozen=True)
class PreparedPublishSession:
    """Shared publish-session preparation result."""

    session_hash: str
    session_url: str | None = None
    registration_session_id: str | None = None
    registration_session_mode: str | None = None


def build_canonical_session_payload(
    *,
    lineage: LineageData,
    git_context: GitContext,
    creator_identity: str,
) -> dict:
    jobs = [
        {
            "command": job.get("command"),
            "job_type": job.get("job_type") or "run",
            "step_number": job.get("step_number"),
            "parent_step_number": _resolve_parent_step_number(job, lineage.jobs),
            "inputs": sorted(
                [
                    {
                        "hash": _canonical_artifact_hash(artifact),
                        "path": artifact.get("path"),
                    }
                    for artifact in job.get("_inputs", [])
                ],
                key=lambda artifact: (
                    str(artifact.get("hash") or ""),
                    str(artifact.get("path") or ""),
                ),
            ),
            "outputs": sorted(
                [
                    {
                        "hash": _canonical_artifact_hash(artifact),
                        "path": artifact.get("path"),
                    }
                    for artifact in job.get("_outputs", [])
                ],
                key=lambda artifact: (
                    str(artifact.get("hash") or ""),
                    str(artifact.get("path") or ""),
                ),
            ),
            "metadata": _normalize_metadata(job.get("metadata")),
        }
        for job in lineage.jobs
    ]
    jobs.sort(key=_job_payload_sort_key)
    return {
        "canonical_version": 1,
        "creator_identity": creator_identity,
        "git": {
            "repo": git_context.repo,
            "commit": git_context.commit,
            "branch": git_context.branch,
        },
        "jobs": jobs,
    }


def build_git_context_from_lineage(lineage: LineageData) -> GitContext:
    pipeline = lineage.pipeline if isinstance(lineage.pipeline, dict) else {}
    return GitContext(
        repo=_pipeline_string(pipeline, "git_repo", "repo"),
        commit=_pipeline_string(pipeline, "git_commit", "commit"),
        branch=_pipeline_string(pipeline, "git_branch", "branch"),
    )


def build_staged_lineage_counts(jobs: list[dict[str, Any]]) -> dict[str, int]:
    """Build lightweight finalize expectations for staged registration-session lineage."""
    return {
        "jobs": len(jobs),
        "inputs": sum(len(job.get("_inputs", [])) for job in jobs),
        "outputs": sum(len(job.get("_outputs", [])) for job in jobs),
    }


def compute_canonical_lineage_session_hash(
    *,
    lineage: LineageData,
    creator_identity: str,
    git_context: GitContext | None = None,
) -> str:
    resolved_git_context = git_context or build_git_context_from_lineage(lineage)
    return compute_canonical_session_hash(
        build_canonical_session_payload(
            lineage=lineage,
            git_context=resolved_git_context,
            creator_identity=creator_identity,
        )
    )


def compute_canonical_jobs_session_hash(
    *,
    jobs: list[dict[str, Any]],
    git_context: GitContext,
    creator_identity: str,
) -> str:
    return compute_canonical_session_hash(
        {
            "canonical_version": 1,
            "creator_identity": creator_identity,
            "git": {
                "repo": git_context.repo,
                "commit": git_context.commit,
                "branch": git_context.branch,
            },
            "jobs": [
                {
                    "command": job.get("command"),
                    "job_type": job.get("job_type") or "run",
                    "step_number": job.get("step_number"),
                    "parent_step_number": _resolve_parent_step_number(job, jobs),
                    "inputs": sorted(
                        [
                            {
                                "hash": _canonical_artifact_hash(artifact),
                                "path": artifact.get("path"),
                            }
                            for artifact in job.get("_inputs", [])
                        ],
                        key=lambda artifact: (
                            str(artifact.get("hash") or ""),
                            str(artifact.get("path") or ""),
                        ),
                    ),
                    "outputs": sorted(
                        [
                            {
                                "hash": _canonical_artifact_hash(artifact),
                                "path": artifact.get("path"),
                            }
                            for artifact in job.get("_outputs", [])
                        ],
                        key=lambda artifact: (
                            str(artifact.get("hash") or ""),
                            str(artifact.get("path") or ""),
                        ),
                    ),
                    "metadata": _normalize_metadata(job.get("metadata")),
                }
                for job in jobs
            ],
        }
    )


def _resolve_parent_step_number(job: dict, all_jobs: list[dict]) -> int | None:
    parent_uid = job.get("remote_parent_job_uid") or job.get("parent_job_uid")
    if not parent_uid:
        return None
    for candidate in all_jobs:
        candidate_uids = {
            candidate.get("job_uid"),
            candidate.get("remote_job_uid"),
        }
        if parent_uid in candidate_uids:
            parent_step = candidate.get("step_number")
            return int(parent_step) if parent_step is not None else None
    return None


def _job_payload_sort_key(job: dict[str, object]) -> tuple[int, str]:
    raw_step = job.get("step_number")
    if isinstance(raw_step, int):
        step_number = raw_step
    else:
        step_number = 0
    raw_command = job.get("command")
    command = raw_command if isinstance(raw_command, str) else ""
    return step_number, command


def _pipeline_string(pipeline: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = pipeline.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _normalize_metadata(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        if not isinstance(parsed, dict):
            return {}
        return dict(sorted(parsed.items()))

    if not isinstance(value, dict):
        return {}
    return dict(sorted(value.items()))


def _canonical_artifact_hash(artifact: dict[str, Any]) -> str | None:
    artifact_hash = artifact.get("artifact_hash")
    if isinstance(artifact_hash, str) and artifact_hash:
        return artifact_hash

    hash_value = artifact.get("hash")
    if isinstance(hash_value, str) and hash_value:
        return hash_value

    hashes = artifact.get("hashes")
    if isinstance(hashes, list):
        for candidate in hashes:
            if not isinstance(candidate, dict):
                continue
            algorithm = candidate.get("algorithm")
            digest = candidate.get("digest")
            if (
                isinstance(algorithm, str)
                and algorithm.lower() == "blake3"
                and isinstance(digest, str)
                and digest
            ):
                return digest
        if hashes:
            first = hashes[0]
            if isinstance(first, dict):
                digest = first.get("digest")
                if isinstance(digest, str) and digest:
                    return digest

    return None


def _registration_session_feature_flags(health_payload: Any) -> tuple[bool, bool]:
    if not isinstance(health_payload, dict):
        return False, False

    features = health_payload.get("features")
    if not isinstance(features, dict):
        return False, False

    registration_sessions = features.get("registration_sessions")
    if not isinstance(registration_sessions, dict):
        return False, False

    anonymous_public = bool(registration_sessions.get("anonymous_public"))
    server_authoritative_hash = bool(
        registration_sessions.get("finalize_server_authoritative_hash")
    )
    return anonymous_public, server_authoritative_hash


def prepare_publish_session(
    *,
    remote_registry: RemoteRegistryTransport | None = None,
    glaas_client: Any | None = None,
    session_service: PublishSessionService | None = None,
    roar_dir: Path,
    session_id: int | None,
    git_context: GitContext,
    logger: ILogger,
    register_with_glaas: bool,
    configured_error: str | None = None,
    session_hash_override: str | None = None,
    lineage: LineageData | None = None,
    creator_identity: str | None = None,
) -> PreparedPublishSession:
    """Compute and optionally register the publish session."""
    resolved_remote_registry = coerce_remote_registry(
        remote_registry=remote_registry,
        glaas_client=glaas_client,
        session_service=session_service,
    )
    resolved_session_service = resolved_remote_registry.session_service
    if resolved_session_service is None:
        raise ValueError("publish session requires a remote registry session service")

    if session_hash_override:
        session_hash = session_hash_override
    elif lineage is not None and creator_identity is not None:
        session_hash = compute_canonical_session_hash(
            build_canonical_session_payload(
                lineage=lineage,
                git_context=git_context,
                creator_identity=creator_identity,
            )
        )
    else:
        if session_id is None:
            raise ValueError("Cannot compute a session hash without a local session id.")
        session_hash = resolved_session_service.compute_session_hash(
            roar_dir=str(roar_dir),
            session_id=session_id,
        )

    logger.debug("Session hash: %s", session_hash[:12])

    if not register_with_glaas:
        return PreparedPublishSession(session_hash=session_hash)

    if configured_error is not None and not resolved_remote_registry.is_configured():
        raise ValueError(configured_error)

    logger.debug("Running GLaaS health check")
    try:
        health_payload = resolved_remote_registry.health_check()
    except Exception as exc:
        logger.debug("GLaaS health check failed: %s", exc)
        raise ValueError(f"GLaaS health check failed: {exc}") from exc

    supports_anonymous_public_reg_sessions, supports_server_authoritative_finalize = (
        _registration_session_feature_flags(health_payload)
    )

    publish_auth = resolved_remote_registry.publish_auth
    access_token = getattr(publish_auth, "access_token", None)
    ssh_auth_available = getattr(publish_auth, "ssh_auth_available", False)
    scope_request = getattr(publish_auth, "scope_request", None)

    has_access_token = isinstance(access_token, str) and bool(access_token.strip())
    has_ssh_auth = ssh_auth_available if isinstance(ssh_auth_available, bool) else False

    anonymous_public_capable = (
        scope_request is None
        and supports_anonymous_public_reg_sessions
        and supports_server_authoritative_finalize
    )
    if has_ssh_auth and not has_access_token and anonymous_public_capable:
        probe_publish_auth = getattr(resolved_remote_registry.client, "probe_publish_auth", None)
        if callable(probe_publish_auth):
            try:
                publish_auth_accepted = probe_publish_auth()
            except Exception as exc:
                logger.debug(
                    "GLaaS publish auth probe was inconclusive; preserving SSH publish path: %s",
                    exc,
                )
            else:
                if publish_auth_accepted is not None:
                    has_ssh_auth = bool(publish_auth_accepted)
                if publish_auth_accepted is False:
                    logger.debug(
                        "Configured SSH publish auth is unavailable remotely; using anonymous public registration session"
                    )

    supports_anonymous_public_path = (
        not has_access_token and not has_ssh_auth and anonymous_public_capable
    )

    should_use_registration_sessions = (
        has_access_token or has_ssh_auth or supports_anonymous_public_path
    )

    if should_use_registration_sessions:
        registration_session_mode = "anonymous_public" if supports_anonymous_public_path else None
        logger.debug(
            "Creating remote registration session with GLaaS%s",
            f" (mode={registration_session_mode})" if registration_session_mode else "",
        )
        session_result = resolved_session_service.create_registration_session(
            client_session_id=None,
            mode=registration_session_mode,
        )
        if not session_result.success:
            logger.debug("Registration session creation failed: %s", session_result.error)
            raise ValueError(f"Registration session creation failed: {session_result.error}")

        logger.debug(
            "Registration session ready: %s",
            session_result.registration_session_id,
        )
        return PreparedPublishSession(
            session_hash=session_hash,
            session_url=None,
            registration_session_id=session_result.registration_session_id,
            registration_session_mode=session_result.registration_session_mode,
        )

    logger.debug("Registering session with GLaaS")
    session_result = resolved_session_service.register(session_hash, git_context)
    if not session_result.success:
        logger.debug("Session registration failed: %s", session_result.error)
        raise ValueError(f"Session registration failed: {session_result.error}")

    logger.debug("Session registered: url=%s", session_result.session_url)
    return PreparedPublishSession(
        session_hash=session_hash,
        session_url=session_result.session_url,
    )
