"""Kubernetes workload manifest rewriting for lineage instrumentation.

Phase 2 scope: exactly one supported training workload per manifest —
``batch/v1`` Job (plain or Indexed), ``jobset.x-k8s.io`` JobSet,
``kubeflow.org/v1`` PyTorchJob, or ``trainer.kubeflow.org`` TrainJob.

The rewriter wraps explicit container commands through the roar pod
entrypoint, injects the env/identity contract, and appends a Secret
carrying the fragment-session credentials so tokens never appear inline
in pod specs. All pods of a workload share one fragment session and one
parent job uid; per-pod identity comes from the downward API plus the
completion-index/node-rank env at runtime.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

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

# Terminal workload conditions, unioned across kinds: Job uses
# Complete/SuccessCriteriaMet + Failed/FailureTarget, JobSet uses
# Completed/Failed, PyTorchJob v1 uses Succeeded/Failed, TrainJob uses
# Complete/Failed.
WORKLOAD_SUCCESS_CONDITIONS = ("Complete", "Completed", "Succeeded", "SuccessCriteriaMet")
WORKLOAD_FAILURE_CONDITIONS = ("Failed", "FailureTarget")


class K8sManifestError(ValueError):
    """Raised when a manifest cannot be instrumented, with actionable detail."""


@dataclass(frozen=True)
class PodSpecRef:
    """A mutable reference to one pod spec inside a workload document."""

    role: str
    spec: dict[str, Any]


@dataclass(frozen=True)
class WorkloadKind:
    kind: str
    api_group: str
    kubectl_resource: str
    # Returns mutable pod-spec references inside the (copied) document.
    # None marks trainer-override style workloads (TrainJob), which have
    # no inline pod template.
    locate_pod_specs: Callable[[dict[str, Any]], list[PodSpecRef]] | None


def _job_pod_specs(doc: dict[str, Any]) -> list[PodSpecRef]:
    spec = _dict_at(doc, ("spec", "template", "spec"))
    return [PodSpecRef(role="", spec=spec)] if spec is not None else []


def _jobset_pod_specs(doc: dict[str, Any]) -> list[PodSpecRef]:
    refs: list[PodSpecRef] = []
    replicated_jobs = _dict_get(doc, "spec").get("replicatedJobs")
    if not isinstance(replicated_jobs, list):
        return refs
    for replicated in replicated_jobs:
        if not isinstance(replicated, dict):
            continue
        role = str(replicated.get("name") or "").strip()
        spec = _dict_at(replicated, ("template", "spec", "template", "spec"))
        if spec is not None:
            refs.append(PodSpecRef(role=role, spec=spec))
    return refs


def _pytorchjob_pod_specs(doc: dict[str, Any]) -> list[PodSpecRef]:
    refs: list[PodSpecRef] = []
    replica_specs = _dict_get(doc, "spec").get("pytorchReplicaSpecs")
    if not isinstance(replica_specs, dict):
        return refs
    for role, replica in replica_specs.items():
        if not isinstance(replica, dict):
            continue
        spec = _dict_at(replica, ("template", "spec"))
        if spec is not None:
            refs.append(PodSpecRef(role=str(role), spec=spec))
    return refs


WORKLOAD_KINDS: tuple[WorkloadKind, ...] = (
    WorkloadKind(
        kind="Job",
        api_group="batch",
        kubectl_resource="jobs.batch",
        locate_pod_specs=_job_pod_specs,
    ),
    WorkloadKind(
        kind="JobSet",
        api_group="jobset.x-k8s.io",
        kubectl_resource="jobsets.jobset.x-k8s.io",
        locate_pod_specs=_jobset_pod_specs,
    ),
    WorkloadKind(
        kind="PyTorchJob",
        api_group="kubeflow.org",
        kubectl_resource="pytorchjobs.kubeflow.org",
        locate_pod_specs=_pytorchjob_pod_specs,
    ),
    WorkloadKind(
        kind="TrainJob",
        api_group="trainer.kubeflow.org",
        kubectl_resource="trainjobs.trainer.kubeflow.org",
        locate_pod_specs=None,
    ),
)


@dataclass(frozen=True)
class K8sManifestRewrite:
    documents: list[dict[str, Any]]
    workload_kind: str
    kubectl_resource: str
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


def workload_kind_for_document(doc: dict[str, Any]) -> WorkloadKind | None:
    api_version = str(doc.get("apiVersion") or "")
    group = api_version.split("/", 1)[0] if "/" in api_version else ""
    if api_version == "batch/v1":
        group = "batch"
    kind = str(doc.get("kind") or "")
    for workload in WORKLOAD_KINDS:
        if workload.kind == kind and workload.api_group == group:
            return workload
    return None


def find_workload_documents(
    documents: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], WorkloadKind]]:
    found: list[tuple[dict[str, Any], WorkloadKind]] = []
    for doc in documents:
        workload = workload_kind_for_document(doc)
        if workload is not None:
            found.append((doc, workload))
    return found


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
    workloads = find_workload_documents(documents)
    if not workloads:
        supported = ", ".join(w.kind for w in WORKLOAD_KINDS)
        raise K8sManifestError(f"no supported training workload found (supported: {supported})")
    if len(workloads) > 1:
        names = [
            f"{workload.kind}/{(doc.get('metadata') or {}).get('name') or '<unnamed>'}"
            for doc, workload in workloads
        ]
        raise K8sManifestError(
            f"manifest contains {len(workloads)} workloads ({', '.join(names)}); "
            "roar instruments exactly one workload per submit"
        )

    source_doc, workload = workloads[0]
    doc_index = next(index for index, doc in enumerate(documents) if doc is source_doc)
    rewritten_documents = [dict(doc) for doc in documents]
    doc = _deep_copy(source_doc)
    rewritten_documents[doc_index] = doc

    metadata = doc.get("metadata")
    if not isinstance(metadata, dict) or not str(metadata.get("name") or "").strip():
        hint = ""
        if isinstance(metadata, dict) and metadata.get("generateName"):
            hint = " (generateName is not supported yet; set a fixed metadata.name)"
        raise K8sManifestError(f"the {workload.kind} needs an explicit metadata.name{hint}")
    workload_name = str(metadata["name"]).strip()
    manifest_namespace = str(metadata.get("namespace") or "").strip()
    namespace = namespace_override or manifest_namespace or "default"

    contract = _EnvContract(
        secret_name=secret_name,
        requirement=requirement,
        cluster_glaas_url=cluster_glaas_url,
        tracer=tracer,
        parent_job_uid=parent_job_uid,
        workload_name=workload_name,
    )

    if workload.locate_pod_specs is None:
        wrapped, skipped = _rewrite_trainjob(doc, workload_name=workload_name, contract=contract)
    else:
        wrapped, skipped = _rewrite_pod_specs(
            workload.locate_pod_specs(doc),
            workload=workload,
            workload_name=workload_name,
            contract=contract,
        )

    if not wrapped:
        raise K8sManifestError(
            f"{workload.kind} {workload_name} has no container with an explicit command; "
            "roar wraps commands it can see — set an explicit command "
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
        workload_kind=workload.kind,
        kubectl_resource=workload.kubectl_resource,
        job_name=workload_name,
        namespace=namespace,
        secret_name=secret_name,
        wrapped_containers=wrapped,
        skipped_containers=skipped,
    )


def dump_manifest_documents(documents: list[dict[str, Any]]) -> str:
    return yaml.safe_dump_all(documents, sort_keys=False)


@dataclass(frozen=True)
class _EnvContract:
    secret_name: str
    requirement: str
    cluster_glaas_url: str
    tracer: str
    parent_job_uid: str
    workload_name: str


def _rewrite_pod_specs(
    pod_specs: list[PodSpecRef],
    *,
    workload: WorkloadKind,
    workload_name: str,
    contract: _EnvContract,
) -> tuple[list[str], list[str]]:
    if not pod_specs:
        raise K8sManifestError(
            f"{workload.kind} {workload_name} has no pod templates roar can instrument"
        )

    wrapped: list[str] = []
    skipped: list[str] = []
    for ref in pod_specs:
        containers = ref.spec.get("containers")
        if not isinstance(containers, list) or not containers:
            raise K8sManifestError(
                f"{workload.kind} {workload_name} pod template "
                f"{ref.role or '<default>'} has no containers"
            )
        for container in containers:
            if not isinstance(container, dict):
                continue
            container_name = str(container.get("name") or "").strip() or "<unnamed>"
            label = f"{ref.role}/{container_name}" if ref.role else container_name
            command = container.get("command")
            if not isinstance(command, list) or not command:
                skipped.append(label)
                continue
            original = [str(part) for part in command]
            original.extend(str(part) for part in container.get("args", []) or [])
            container["command"] = _wrapped_command(original, requirement=contract.requirement)
            container.pop("args", None)
            _inject_env_contract(
                container.setdefault("env", []),
                contract=contract,
                container_name=container_name,
                role=ref.role,
            )
            wrapped.append(label)
    return wrapped, skipped


def _rewrite_trainjob(
    doc: dict[str, Any],
    *,
    workload_name: str,
    contract: _EnvContract,
) -> tuple[list[str], list[str]]:
    """TrainJob has no inline pod template; wrap the trainer override.

    The trainer command/args/env override the runtime blueprint's ``node``
    container. Runtimes whose image entrypoint is torchrun (driven by
    operator-injected ``PET_*`` env) expose no command roar can see, so an
    explicit ``spec.trainer.command`` is required.
    """
    trainer = _dict_get(doc, "spec").get("trainer")
    if not isinstance(trainer, dict):
        raise K8sManifestError(
            f"TrainJob {workload_name} has no spec.trainer override; "
            "roar needs spec.trainer.command to wrap (runtime-image entrypoints "
            "are not visible at submit time)"
        )
    command = trainer.get("command")
    if not isinstance(command, list) or not command:
        raise K8sManifestError(
            f"TrainJob {workload_name} has no spec.trainer.command; "
            "set an explicit command to instrument (the runtime image's "
            "torchrun entrypoint is not visible at submit time)"
        )

    original = [str(part) for part in command]
    original.extend(str(part) for part in trainer.get("args", []) or [])
    trainer["command"] = _wrapped_command(original, requirement=contract.requirement)
    trainer.pop("args", None)

    env = trainer.setdefault("env", [])
    if not isinstance(env, list):
        raise K8sManifestError(f"TrainJob {workload_name} has a non-list spec.trainer.env")
    _inject_env_contract(env, contract=contract, container_name="node", role="")
    return ["node"], []


def _wrapped_command(original: list[str], *, requirement: str) -> list[str]:
    script = _POD_WRAPPER_TEMPLATE.format(requirement=shlex.quote(requirement))
    return ["/bin/sh", "-c", script, "roar-k8s", *original]


def _inject_env_contract(
    env: list[Any],
    *,
    contract: _EnvContract,
    container_name: str,
    role: str,
) -> None:
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
                    "valueFrom": {"secretKeyRef": {"name": contract.secret_name, "key": key}},
                }
            )

    task_name = contract.workload_name
    if role:
        task_name = f"{task_name}/{role}"
    task_name = f"{task_name}/{container_name}"

    add_value("ROAR_EXECUTION_BACKEND", "k8s")
    add_value("ROAR_NO_TELEMETRY", "1")
    add_value("ROAR_K8S_TRACER", contract.tracer)
    add_value("GLAAS_URL", contract.cluster_glaas_url)
    add_value("ROAR_K8S_PARENT_JOB_UID", contract.parent_job_uid)
    add_value("ROAR_K8S_JOB_NAME", contract.workload_name)
    add_value("ROAR_K8S_CONTAINER", container_name)
    add_value("ROAR_K8S_TASK_NAME", task_name)
    add_secret_ref("ROAR_SESSION_ID", "session_id")
    add_secret_ref("ROAR_FRAGMENT_TOKEN", "token")
    add_field_ref("ROAR_K8S_NAMESPACE", "metadata.namespace")
    add_field_ref("ROAR_K8S_POD_NAME", "metadata.name")
    add_field_ref("ROAR_K8S_POD_UID", "metadata.uid")
    add_field_ref("ROAR_K8S_NODE_NAME", "spec.nodeName")


def _dict_get(doc: dict[str, Any], key: str) -> dict[str, Any]:
    value = doc.get(key)
    return value if isinstance(value, dict) else {}


def _dict_at(root: dict[str, Any], path: tuple[str, ...]) -> dict[str, Any] | None:
    node: Any = root
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
    return node if isinstance(node, dict) else None


def _deep_copy(document: dict[str, Any]) -> dict[str, Any]:
    import copy

    return copy.deepcopy(document)


__all__ = [
    "WORKLOAD_FAILURE_CONDITIONS",
    "WORKLOAD_KINDS",
    "WORKLOAD_SUCCESS_CONDITIONS",
    "K8sManifestError",
    "K8sManifestRewrite",
    "WorkloadKind",
    "dump_manifest_documents",
    "find_workload_documents",
    "load_manifest_documents",
    "rewrite_manifest_for_lineage",
    "workload_kind_for_document",
]
