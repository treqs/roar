"""Canonical session-payload normalization and hashing utilities."""

from __future__ import annotations

import hashlib
import json
from typing import Any

_ALLOWED_TOP_LEVEL_KEYS = {"canonical_version", "creator_identity", "git", "jobs"}


def normalize_creator_identity(value: str | None) -> str:
    normalized = (value or "").strip()
    return normalized or "anonymous"


def normalize_job(job: dict[str, Any]) -> dict[str, Any]:
    normalized_inputs = sorted(
        (_normalize_artifact_ref(item) for item in job.get("inputs") or []),
        key=lambda item: (item["hash"], item["path"]),
    )
    normalized_outputs = sorted(
        (_normalize_artifact_ref(item) for item in job.get("outputs") or []),
        key=lambda item: (item["hash"], item["path"]),
    )
    metadata = job.get("metadata")
    return {
        "command": job.get("command"),
        "job_type": job.get("job_type"),
        "step_number": job.get("step_number"),
        "parent_step_number": job.get("parent_step_number"),
        "inputs": normalized_inputs,
        "outputs": normalized_outputs,
        "metadata": _normalize_json_value(metadata if isinstance(metadata, dict) else {}),
    }


def normalize_jobs(jobs: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    normalized = [normalize_job(job) for job in jobs]
    return sorted(normalized, key=_job_sort_key)


def serialize_canonical_session_payload(payload: dict[str, Any]) -> str:
    canonical_payload = {
        "canonical_version": payload.get("canonical_version", 1),
        "creator_identity": normalize_creator_identity(payload.get("creator_identity")),
        "git": _normalize_git(payload.get("git")),
        "jobs": normalize_jobs(list(payload.get("jobs") or [])),
    }
    filtered = {key: canonical_payload[key] for key in _ALLOWED_TOP_LEVEL_KEYS}
    return json.dumps(filtered, sort_keys=True, separators=(",", ":"))


def compute_canonical_session_hash(payload: dict[str, Any]) -> str:
    serialized = serialize_canonical_session_payload(payload)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _normalize_artifact_ref(item: dict[str, Any]) -> dict[str, str | None]:
    return {
        "hash": _normalize_optional_string(item.get("hash")),
        "path": _normalize_optional_string(item.get("path")),
    }


def _normalize_git(git: Any) -> dict[str, str | None]:
    git_payload = git if isinstance(git, dict) else {}
    return {
        "repo": _normalize_optional_string(git_payload.get("repo")),
        "commit": _normalize_optional_string(git_payload.get("commit")),
        "branch": _normalize_optional_string(git_payload.get("branch")),
    }


def _normalize_optional_string(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _normalize_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _normalize_json_value(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_normalize_json_value(item) for item in value]
    return value


def _job_sort_key(job: dict[str, Any]) -> tuple[Any, str]:
    return (job.get("step_number"), json.dumps(job, sort_keys=True, separators=(",", ":")))
