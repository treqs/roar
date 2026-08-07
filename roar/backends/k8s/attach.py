"""Attach local roar lineage to an already-submitted Kubernetes workload.

The CI/fire-and-forget flow: someone (or something) submitted an
instrumented workload; ``roar k8s attach`` recovers the fragment-session
credentials and parent job identity from the cluster object itself, waits
for completion if asked, records a local attach job, and reconstitutes
the streamed fragments into the local ``.roar/roar.db``.
"""

from __future__ import annotations

import base64
import json
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from roar.backends.k8s.config import load_k8s_backend_config
from roar.backends.k8s.fragment_reconstituter import create_k8s_fragment_reconstituter
from roar.backends.k8s.manifest import (
    WORKLOAD_KINDS,
    WorkloadKind,
    workload_kind_for_document,
)
from roar.backends.k8s.workload_wait import (
    get_workload_document,
    terminal_condition,
    wait_for_workload_completion,
)
from roar.core.bootstrap import bootstrap
from roar.core.operation_metadata import build_operation_metadata_json
from roar.db.context import create_database_context
from roar.execution.fragments.sessions import load_fragment_session
from roar.execution.recording import LocalJobRecorder


class K8sAttachError(RuntimeError):
    """Raised when attach cannot recover lineage, with actionable detail."""


@dataclass(frozen=True)
class K8sAttachResult:
    exit_code: int
    workload: str
    session_id: str
    jobs_merged: int
    artifacts_merged: int
    fragments_processed: int


_KIND_ALIASES = {
    "job": "jobs.batch",
    "jobs": "jobs.batch",
    "jobset": "jobsets.jobset.x-k8s.io",
    "jobsets": "jobsets.jobset.x-k8s.io",
    "pytorchjob": "pytorchjobs.kubeflow.org",
    "pytorchjobs": "pytorchjobs.kubeflow.org",
    "trainjob": "trainjobs.trainer.kubeflow.org",
    "trainjobs": "trainjobs.trainer.kubeflow.org",
    "rayjob": "rayjobs.ray.io",
    "rayjobs": "rayjobs.ray.io",
}


def attach_k8s_workload(
    *,
    roar_dir: Path,
    repo_root: str,
    workload: str,
    namespace: str,
    kubectl_binary: str = "kubectl",
    global_flags: list[str] | None = None,
    wait: bool | None = None,
    session_file: Path | None = None,
) -> K8sAttachResult:
    bootstrap(roar_dir)
    started_at = time.time()
    flags = list(global_flags or [])
    config = load_k8s_backend_config(start_dir=repo_root)

    name, document, workload_kind = _fetch_workload(
        workload=workload,
        namespace=namespace,
        kubectl_binary=kubectl_binary,
        global_flags=flags,
    )
    kubectl_resource = workload_kind.kubectl_resource

    parent_job_uid, secret_name = _recover_identity(document, workload_kind)
    if not parent_job_uid or not secret_name:
        raise K8sAttachError(
            f"{kubectl_resource}/{name} was not instrumented by roar "
            "(no fragment-session env found); submit through "
            "`roar run kubectl apply -f ...` or `roar k8s prepare`"
        )

    session_id, token = _resolve_session_credentials(
        roar_dir=roar_dir,
        session_file=session_file,
        secret_name=secret_name,
        namespace=namespace,
        kubectl_binary=kubectl_binary,
        global_flags=flags,
    )

    should_wait = bool(config.get("wait_for_completion", True)) if wait is None else wait
    succeeded, state = terminal_condition(document)
    wait_payload: dict[str, Any] | None = None
    if succeeded is None and should_wait:
        wait_ok, wait_payload = wait_for_workload_completion(
            kubectl_binary=kubectl_binary,
            global_flags=flags,
            kubectl_resource=kubectl_resource,
            name=name,
            namespace=namespace,
            timeout_seconds=int(config.get("wait_timeout_seconds", 30 * 60)),
            poll_interval_seconds=float(config.get("poll_interval_seconds", 5.0)),
        )
        succeeded = wait_ok

    exit_code = 0 if succeeded in (True, None) else 1
    payload = {
        "workload_kind": workload_kind.kind,
        "kubectl_resource": kubectl_resource,
        "name": name,
        "namespace": namespace,
        "secret_name": secret_name,
        "session_id": session_id,
        "parent_job_uid": parent_job_uid,
        "terminal_state": state or None,
        "wait": wait_payload,
    }

    _ensure_local_attach_job(
        roar_dir=roar_dir,
        parent_job_uid=parent_job_uid,
        command=shlex.join(["roar", "k8s", "attach", f"{kubectl_resource}/{name}"]),
        started_at=started_at,
        exit_code=exit_code,
        payload=payload,
    )

    from roar.integrations.glaas import get_glaas_url

    glaas_url = get_glaas_url()
    if not glaas_url:
        raise K8sAttachError("GLaaS is not configured; set glaas.url to fetch fragments")

    result = _reconstituter_factory(workload_kind)(
        session_id,
        token,
        str(glaas_url),
        roar_dir / "roar.db",
    ).reconstitute(driver_job_uid=parent_job_uid)

    return K8sAttachResult(
        exit_code=exit_code,
        workload=f"{kubectl_resource}/{name}",
        session_id=session_id,
        jobs_merged=result.jobs_merged,
        artifacts_merged=result.artifacts_merged,
        fragments_processed=result.fragments_processed,
    )


def _fetch_workload(
    *,
    workload: str,
    namespace: str,
    kubectl_binary: str,
    global_flags: list[str],
) -> tuple[str, dict[str, Any], WorkloadKind]:
    name = workload
    candidates: list[str]
    if "/" in workload:
        kind_part, name = workload.split("/", 1)
        resource = _KIND_ALIASES.get(kind_part.lower().split(".")[0])
        if resource is None:
            raise K8sAttachError(
                f"unknown workload kind {kind_part!r}; "
                f"expected one of {', '.join(sorted(set(_KIND_ALIASES)))}"
            )
        candidates = [resource]
    else:
        candidates = [kind.kubectl_resource for kind in WORKLOAD_KINDS]

    errors: list[str] = []
    for resource in candidates:
        document, error = get_workload_document(
            kubectl_binary=kubectl_binary,
            global_flags=global_flags,
            kubectl_resource=resource,
            name=name,
            namespace=namespace,
        )
        if document is None:
            if error:
                errors.append(f"{resource}: {error}")
            continue
        workload_kind = workload_kind_for_document(document)
        if workload_kind is None:
            errors.append(f"{resource}: unsupported workload document")
            continue
        return name, document, workload_kind

    detail = "; ".join(errors) if errors else "not found under any supported workload kind"
    raise K8sAttachError(f"cannot fetch workload {workload!r} in namespace {namespace}: {detail}")


def _recover_identity(
    document: dict[str, Any],
    workload_kind: WorkloadKind,
) -> tuple[str, str]:
    """Return (parent_job_uid, secret_name) from the cluster object."""
    env_entries = _instrumented_env_entries(document, workload_kind)
    secret_name = _session_secret_name(env_entries)

    if workload_kind.kind == "RayJob":
        # RayJob carries the parent uid in the Ray env contract
        # (runtimeEnvYAML env_vars.ROAR_JOB_ID), not pod env.
        import yaml  # type: ignore[import-untyped]

        raw = document.get("spec", {}).get("runtimeEnvYAML")
        try:
            runtime_env = yaml.safe_load(raw) if isinstance(raw, str) else None
        except yaml.YAMLError:
            runtime_env = None
        env_vars = runtime_env.get("env_vars") if isinstance(runtime_env, dict) else None
        parent = str((env_vars or {}).get("ROAR_JOB_ID") or "").strip()
        return parent, secret_name

    return _env_value(env_entries, "ROAR_K8S_PARENT_JOB_UID"), secret_name


def _reconstituter_factory(workload_kind: WorkloadKind):
    if workload_kind.kind == "RayJob":
        # RayJob fragments are Ray TaskFragments; use the Ray reconstituter.
        from roar.execution.framework.registry import get_execution_backend

        backend = get_execution_backend("ray")
        distributed = backend.distributed
        if distributed is None or distributed.fragment_reconstitution is None:
            raise K8sAttachError("ray backend has no fragment reconstitution adapter")
        return distributed.fragment_reconstitution.create_reconstituter
    return create_k8s_fragment_reconstituter


def _instrumented_env_entries(
    document: dict[str, Any],
    workload_kind: WorkloadKind,
) -> list[dict[str, Any]]:
    if workload_kind.locate_pod_specs is None:
        trainer = document.get("spec", {}).get("trainer")
        env = trainer.get("env") if isinstance(trainer, dict) else None
        return [entry for entry in env or [] if isinstance(entry, dict)]

    def _is_instrumented(entry: dict[str, Any]) -> bool:
        if entry.get("name") == "ROAR_K8S_PARENT_JOB_UID":
            return True
        return entry.get("name") == "ROAR_SESSION_ID" and bool(
            (entry.get("valueFrom") or {}).get("secretKeyRef")
        )

    for ref in workload_kind.locate_pod_specs(document):
        containers = ref.spec.get("containers")
        if not isinstance(containers, list):
            continue
        for container in containers:
            if not isinstance(container, dict):
                continue
            env = [entry for entry in container.get("env") or [] if isinstance(entry, dict)]
            if any(_is_instrumented(entry) for entry in env):
                return env
    return []


def _env_value(env_entries: list[dict[str, Any]], name: str) -> str:
    for entry in env_entries:
        if entry.get("name") == name:
            return str(entry.get("value") or "").strip()
    return ""


def _session_secret_name(env_entries: list[dict[str, Any]]) -> str:
    for entry in env_entries:
        if entry.get("name") != "ROAR_SESSION_ID":
            continue
        secret_ref = (entry.get("valueFrom") or {}).get("secretKeyRef") or {}
        return str(secret_ref.get("name") or "").strip()
    return ""


def _resolve_session_credentials(
    *,
    roar_dir: Path,
    session_file: Path | None,
    secret_name: str,
    namespace: str,
    kubectl_binary: str,
    global_flags: list[str],
) -> tuple[str, str]:
    if session_file is not None:
        payload = json.loads(session_file.read_text(encoding="utf-8"))
        session_id = str(payload.get("session_id") or "").strip()
        token = str(payload.get("token") or "").strip()
        if not session_id or not token:
            raise K8sAttachError(f"session file {session_file} is missing session_id/token")
        return session_id, token

    # Prefer a locally saved key (attach on the submitting machine), then
    # fall back to reading the cluster Secret (attach from anywhere with RBAC).
    session_id_hint = secret_name.removeprefix("roar-fragment-")
    local = _find_local_session(roar_dir, session_id_hint)
    if local is not None:
        return local

    command = [
        kubectl_binary,
        *global_flags,
        "get",
        "secret",
        secret_name,
        "-n",
        namespace,
        "-o",
        "json",
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise K8sAttachError(
            f"cannot read Secret {secret_name} in namespace {namespace} "
            f"({result.stderr.strip()}); pass --session-file if you have the local key"
        )
    data = json.loads(result.stdout).get("data") or {}
    try:
        session_id = base64.b64decode(str(data.get("session_id") or "")).decode("utf-8").strip()
        token = base64.b64decode(str(data.get("token") or "")).decode("utf-8").strip()
    except Exception as exc:
        raise K8sAttachError(f"Secret {secret_name} has invalid session data: {exc}") from exc
    if not session_id or not token:
        raise K8sAttachError(f"Secret {secret_name} is missing session_id/token keys")
    return session_id, token


def _find_local_session(roar_dir: Path, session_id_prefix: str) -> tuple[str, str] | None:
    if not session_id_prefix:
        return None
    sessions_dir = roar_dir / "fragment-sessions"
    if not sessions_dir.is_dir():
        return None
    for key_path in sorted(sessions_dir.glob(f"{session_id_prefix}*.key")):
        try:
            payload = load_fragment_session(roar_dir, key_path.stem)
        except Exception:
            continue
        session_id = str(payload.get("session_id") or "").strip()
        token = str(payload.get("token") or "").strip()
        if session_id and token:
            return session_id, token
    return None


def _ensure_local_attach_job(
    *,
    roar_dir: Path,
    parent_job_uid: str,
    command: str,
    started_at: float,
    exit_code: int,
    payload: dict[str, Any],
) -> None:
    with create_database_context(roar_dir) as db_ctx:
        existing = db_ctx.jobs.get_by_uid(parent_job_uid)
        if existing is not None:
            return
        session_id = db_ctx.sessions.get_or_create_active()
        LocalJobRecorder().record(
            db_ctx,
            command=command,
            timestamp=started_at,
            metadata=build_operation_metadata_json("k8s_attach", payload),
            execution_backend="k8s",
            execution_role="attach",
            job_type="run",
            input_artifacts=[],
            output_artifacts=[],
            duration_seconds=max(0.0, time.time() - started_at),
            exit_code=exit_code,
            session_id=session_id,
            job_uid=parent_job_uid,
        )
        db_ctx.commit()


__all__ = [
    "K8sAttachError",
    "K8sAttachResult",
    "attach_k8s_workload",
]
