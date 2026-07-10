"""Kubernetes Job manifest rewriting for lineage instrumentation.

Phase 1 scope: exactly one ``batch/v1`` Job per manifest (Indexed or
plain). The rewriter wraps explicit container commands through the roar
pod entrypoint, injects the env/identity contract, and appends a Secret
carrying the fragment-session credentials so tokens never appear inline
in pod specs.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

_JOB_API_VERSION = "batch/v1"
_JOB_KIND = "Job"

# sh -c script: "$0" is the synthetic argv0, "$@" is the original
# command+args. Lineage is best-effort by design: any failure to stage
# the roar runtime falls back to running the original command
# uninstrumented rather than failing the training job.
_POD_WRAPPER_TEMPLATE = """\
run_fallback() {{ echo "[roar-k8s] lineage runtime unavailable; running uninstrumented" >&2; exec "$@"; }}
command -v python3 >/dev/null 2>&1 || run_fallback "$@"
python3 -m pip install --quiet {requirement} || run_fallback "$@"
exec python3 -m roar.backends.k8s.pod_entry "$@"
"""


class K8sManifestError(ValueError):
    """Raised when a manifest cannot be instrumented, with actionable detail."""


@dataclass(frozen=True)
class K8sManifestRewrite:
    documents: list[dict[str, Any]]
    job_name: str
    namespace: str
    secret_name: str
    wrapped_containers: list[str] = field(default_factory=list)
    skipped_containers: list[str] = field(default_factory=list)


def load_manifest_documents(path: Path) -> list[dict[str, Any]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise K8sManifestError(f"cannot read manifest {path}: {exc}") from exc

    try:
        documents = [doc for doc in yaml.safe_load_all(raw) if doc is not None]
    except yaml.YAMLError as exc:
        raise K8sManifestError(f"invalid YAML in manifest {path}: {exc}") from exc

    return [doc for doc in documents if isinstance(doc, dict)]


def find_job_documents(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        doc
        for doc in documents
        if str(doc.get("apiVersion") or "") == _JOB_API_VERSION
        and str(doc.get("kind") or "") == _JOB_KIND
    ]


def rewrite_manifest_for_lineage(
    documents: list[dict[str, Any]],
    *,
    secret_name: str,
    session_id: str | None,
    fragment_token: str | None,
    requirement: str,
    cluster_glaas_url: str,
    tracer: str,
    parent_job_uid: str,
    namespace_override: str | None = None,
) -> K8sManifestRewrite:
    """Return a rewritten copy of ``documents`` with lineage instrumentation.

    When ``session_id``/``fragment_token`` are provided, a Secret document is
    appended and referenced from the wrapped containers; otherwise the env
    contract still points at ``secret_name`` (the ``roar k8s prepare`` flow,
    where the user creates the Secret out of band).
    """
    jobs = find_job_documents(documents)
    if not jobs:
        raise K8sManifestError(
            "no batch/v1 Job found in manifest; Phase 1 instruments plain Jobs only"
        )
    if len(jobs) > 1:
        names = [str((doc.get("metadata") or {}).get("name") or "<unnamed>") for doc in jobs]
        raise K8sManifestError(
            f"manifest contains {len(jobs)} Jobs ({', '.join(names)}); "
            "Phase 1 instruments exactly one Job per submit"
        )

    job_index = next(index for index, doc in enumerate(documents) if doc is jobs[0])
    rewritten_documents = [dict(doc) for doc in documents]
    job = _deep_copy(jobs[0])
    rewritten_documents[job_index] = job

    metadata = job.get("metadata")
    if not isinstance(metadata, dict) or not str(metadata.get("name") or "").strip():
        hint = ""
        if isinstance(metadata, dict) and metadata.get("generateName"):
            hint = " (generateName is not supported yet; set a fixed metadata.name)"
        raise K8sManifestError(f"the Job needs an explicit metadata.name{hint}")
    job_name = str(metadata["name"]).strip()
    manifest_namespace = str(metadata.get("namespace") or "").strip()
    namespace = namespace_override or manifest_namespace or "default"

    pod_spec = _require_dict_path(job, ("spec", "template", "spec"), job_name=job_name)
    containers = pod_spec.get("containers")
    if not isinstance(containers, list) or not containers:
        raise K8sManifestError(f"Job {job_name} has no spec.template.spec.containers")

    wrapped: list[str] = []
    skipped: list[str] = []
    for container in containers:
        if not isinstance(container, dict):
            continue
        container_name = str(container.get("name") or "").strip() or "<unnamed>"
        command = container.get("command")
        if not isinstance(command, list) or not command:
            skipped.append(container_name)
            continue
        _wrap_container(
            container,
            job_name=job_name,
            secret_name=secret_name,
            requirement=requirement,
            cluster_glaas_url=cluster_glaas_url,
            tracer=tracer,
            parent_job_uid=parent_job_uid,
        )
        wrapped.append(container_name)

    if not wrapped:
        raise K8sManifestError(
            f"Job {job_name} has no container with an explicit command; "
            "roar wraps commands it can see — set spec.template.spec.containers[].command "
            "(images relying on ENTRYPOINT are not supported yet)"
        )

    if session_id and fragment_token:
        secret_doc: dict[str, Any] = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": secret_name,
                "labels": {"app.kubernetes.io/managed-by": "roar"},
            },
            "type": "Opaque",
            "stringData": {
                "session_id": session_id,
                "token": fragment_token,
            },
        }
        if manifest_namespace:
            secret_doc["metadata"]["namespace"] = manifest_namespace
        rewritten_documents.insert(0, secret_doc)

    return K8sManifestRewrite(
        documents=rewritten_documents,
        job_name=job_name,
        namespace=namespace,
        secret_name=secret_name,
        wrapped_containers=wrapped,
        skipped_containers=skipped,
    )


def dump_manifest_documents(documents: list[dict[str, Any]]) -> str:
    return yaml.safe_dump_all(documents, sort_keys=False)


def _wrap_container(
    container: dict[str, Any],
    *,
    job_name: str,
    secret_name: str,
    requirement: str,
    cluster_glaas_url: str,
    tracer: str,
    parent_job_uid: str,
) -> None:
    container_name = str(container.get("name") or "").strip() or "main"
    original = [str(part) for part in container.get("command", [])]
    original.extend(str(part) for part in container.get("args", []) or [])

    script = _POD_WRAPPER_TEMPLATE.format(requirement=shlex.quote(requirement))
    container["command"] = ["/bin/sh", "-c", script, "roar-k8s", *original]
    container.pop("args", None)

    env = container.setdefault("env", [])
    if not isinstance(env, list):
        raise K8sManifestError(f"container {container_name} has a non-list env block")
    existing_names = {
        str(entry.get("name")) for entry in env if isinstance(entry, dict) and entry.get("name")
    }

    def add_value(name: str, value: str) -> None:
        if name not in existing_names:
            env.append({"name": name, "value": value})

    def add_field_ref(name: str, field_path: str) -> None:
        if name not in existing_names:
            env.append({"name": name, "valueFrom": {"fieldRef": {"fieldPath": field_path}}})

    def add_secret_ref(name: str, key: str) -> None:
        if name not in existing_names:
            env.append(
                {
                    "name": name,
                    "valueFrom": {"secretKeyRef": {"name": secret_name, "key": key}},
                }
            )

    add_value("ROAR_EXECUTION_BACKEND", "k8s")
    add_value("ROAR_NO_TELEMETRY", "1")
    add_value("ROAR_K8S_TRACER", tracer)
    add_value("GLAAS_URL", cluster_glaas_url)
    add_value("ROAR_K8S_PARENT_JOB_UID", parent_job_uid)
    add_value("ROAR_K8S_JOB_NAME", job_name)
    add_value("ROAR_K8S_CONTAINER", container_name)
    add_value("ROAR_K8S_TASK_NAME", f"{job_name}/{container_name}")
    add_secret_ref("ROAR_SESSION_ID", "session_id")
    add_secret_ref("ROAR_FRAGMENT_TOKEN", "token")
    add_field_ref("ROAR_K8S_NAMESPACE", "metadata.namespace")
    add_field_ref("ROAR_K8S_POD_NAME", "metadata.name")
    add_field_ref("ROAR_K8S_POD_UID", "metadata.uid")
    add_field_ref("ROAR_K8S_NODE_NAME", "spec.nodeName")


def _require_dict_path(
    root: dict[str, Any],
    path: tuple[str, ...],
    *,
    job_name: str,
) -> dict[str, Any]:
    node: Any = root
    walked: list[str] = []
    for key in path:
        walked.append(key)
        node = node.get(key) if isinstance(node, dict) else None
        if not isinstance(node, dict):
            raise K8sManifestError(f"Job {job_name} is missing {'.'.join(walked)}")
    return node


def _deep_copy(document: dict[str, Any]) -> dict[str, Any]:
    import copy

    return copy.deepcopy(document)


__all__ = [
    "K8sManifestError",
    "K8sManifestRewrite",
    "dump_manifest_documents",
    "find_job_documents",
    "load_manifest_documents",
    "rewrite_manifest_for_lineage",
]
