"""RayJob delegation: k8s manifest plumbing, Ray backend runtime semantics.

KubeRay overwrites container commands with ``ray start --block`` and runs
user code in Ray worker actors, so the generic command-wrapping adapter
cannot see it. Instead, a RayJob rewrite reuses the Ray backend's proven
instrumentation surface:

- ``spec.entrypoint`` is wrapped through the Ray driver entrypoint,
- ``spec.runtimeEnvYAML`` gains the roar pip requirement, the worker
  setup hook, and the Ray env contract (``ROAR_EXECUTION_BACKEND=ray``),
- fragment-session credentials go into the RayCluster pod templates as
  Secret refs (never plaintext in the CR),

and reconstitution is delegated to the Ray backend's fragment
reconstituter, since the streamed fragments are Ray ``TaskFragment``
payloads.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import yaml  # type: ignore[import-untyped]

from roar.backends.ray.env_contract import merge_worker_bootstrap_env
from roar.backends.ray.submit_context import (
    build_submit_instrumentation_context,
    build_submit_source_environ,
)
from roar.execution.framework.contract import ROAR_EXECUTION_BACKEND_ENV

if TYPE_CHECKING:
    from roar.backends.k8s.manifest import _EnvContract

_DRIVER_ENTRYPOINT_PREFIX = "python -m roar.execution.runtime.driver_entrypoint -- "
_WORKER_SETUP_HOOK = "roar.execution.runtime.worker_bootstrap.startup"

RAYJOB_SUCCESS_STATUSES = frozenset({"SUCCEEDED"})
RAYJOB_FAILURE_STATUSES = frozenset({"FAILED", "STOPPED"})


def rewrite_rayjob_for_lineage(
    doc: dict[str, Any],
    *,
    workload_name: str,
    contract: _EnvContract,
) -> tuple[list[str], list[str]]:
    """Instrument a RayJob document in place; returns (wrapped, skipped)."""
    from roar.backends.k8s.manifest import K8sManifestError

    spec = doc.get("spec")
    if not isinstance(spec, dict):
        raise K8sManifestError(f"RayJob {workload_name} has no spec")

    entrypoint = str(spec.get("entrypoint") or "").strip()
    if not entrypoint:
        raise K8sManifestError(
            f"RayJob {workload_name} has no spec.entrypoint; roar instruments "
            "job-mode RayJobs (interactive mode is not supported)"
        )
    if _DRIVER_ENTRYPOINT_PREFIX.strip() not in entrypoint:
        spec["entrypoint"] = _DRIVER_ENTRYPOINT_PREFIX + entrypoint

    spec["runtimeEnvYAML"] = _merged_runtime_env_yaml(
        spec.get("runtimeEnvYAML"),
        workload_name=workload_name,
        contract=contract,
    )

    wrapped = ["entrypoint"]
    for ref in rayjob_pod_specs(doc):
        containers = ref.spec.get("containers")
        if not isinstance(containers, list):
            continue
        for container in containers:
            if isinstance(container, dict):
                _inject_secret_env(container, secret_name=contract.secret_name)
        wrapped.append(f"{ref.role}/pods")
    return wrapped, []


def rayjob_pod_specs(doc: dict[str, Any]):
    """Head/worker pod-spec refs (used for Secret env and attach recovery)."""
    from roar.backends.k8s.manifest import PodSpecRef, _dict_at, _dict_get

    refs = []
    cluster_spec = _dict_get(doc.get("spec") or {}, "rayClusterSpec")
    head_spec = _dict_at(cluster_spec, ("headGroupSpec", "template", "spec"))
    if head_spec is not None:
        refs.append(PodSpecRef(role="head", spec=head_spec))
    for group in cluster_spec.get("workerGroupSpecs") or []:
        if not isinstance(group, dict):
            continue
        group_spec = _dict_at(group, ("template", "spec"))
        if group_spec is not None:
            refs.append(PodSpecRef(role=str(group.get("groupName") or "workers"), spec=group_spec))
    return refs


def _merged_runtime_env_yaml(
    raw: Any,
    *,
    workload_name: str,
    contract: _EnvContract,
) -> str:
    from roar.backends.k8s.manifest import K8sManifestError

    runtime_env: dict[str, Any] = {}
    if isinstance(raw, str) and raw.strip():
        try:
            loaded = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise K8sManifestError(
                f"RayJob {workload_name} has invalid runtimeEnvYAML: {exc}"
            ) from exc
        if isinstance(loaded, dict):
            runtime_env = loaded

    pip = runtime_env.get("pip")
    packages: list[str] = []
    if isinstance(pip, dict):
        packages = [str(item) for item in pip.get("packages") or []]
    elif isinstance(pip, list):
        packages = [str(item) for item in pip]
    if contract.requirement and contract.requirement not in packages:
        packages.append(contract.requirement)
    runtime_env["pip"] = packages

    runtime_env["worker_process_setup_hook"] = _WORKER_SETUP_HOOK

    # The Ray env contract, keyed to the k8s parent uid so Ray fragments
    # attach to the recorded submit job.
    context = build_submit_instrumentation_context(
        os.environ,
        cwd=os.getcwd(),
        host_glaas_url=None,
        job_id=contract.parent_job_uid,
    )
    env_vars = merge_worker_bootstrap_env(
        dict(runtime_env.get("env_vars") or {}),
        build_submit_source_environ(context),
        job_id=context.job_id,
        overwrite_existing=True,
    )
    env_vars[ROAR_EXECUTION_BACKEND_ENV] = "ray"
    env_vars["GLAAS_URL"] = contract.cluster_glaas_url
    # Per the proxy decision, node agents/proxy sidecars stay off for
    # RayJob delegation v1; in-process hooks are the capture surface.
    env_vars["ROAR_RAY_NODE_AGENTS"] = "0"
    # No proxy runs in the pods, so the merged local-proxy redirect would
    # point user S3 traffic at a dead localhost port — strip it.
    env_vars.pop("AWS_ENDPOINT_URL", None)
    env_vars.pop("ROAR_PROXY_PORT", None)
    # ROAR_WRAP is deliberately NOT set: in Ray pip virtualenvs the
    # roar_inject.pth fires at worker interpreter startup before the
    # virtualenv's site-packages are importable, and the sitecustomize
    # ABI-repair path then blows the worker registration timeout
    # (observed live: supervisor start -> hang -> kill -> retry forever).
    # worker_process_setup_hook is the capture surface for RayJob v1.
    env_vars.pop("ROAR_WRAP", None)
    # Credentials come from the pod-level Secret refs, never the CR.
    env_vars.pop("ROAR_SESSION_ID", None)
    env_vars.pop("ROAR_FRAGMENT_TOKEN", None)
    runtime_env["env_vars"] = env_vars

    return yaml.safe_dump(runtime_env, sort_keys=False)


def _inject_secret_env(container: dict[str, Any], *, secret_name: str) -> None:
    env = container.setdefault("env", [])
    if not isinstance(env, list):
        return
    existing = {
        str(entry.get("name")) for entry in env if isinstance(entry, dict) and entry.get("name")
    }
    for name, key in (("ROAR_SESSION_ID", "session_id"), ("ROAR_FRAGMENT_TOKEN", "token")):
        if name not in existing:
            env.append(
                {
                    "name": name,
                    "valueFrom": {"secretKeyRef": {"name": secret_name, "key": key}},
                }
            )


def rayjob_terminal_status(document: dict[str, Any]) -> tuple[bool | None, str]:
    """RayJob signals completion via status.jobStatus, not conditions."""
    status = document.get("status")
    job_status = str((status or {}).get("jobStatus") or "").strip().upper()
    if job_status in RAYJOB_SUCCESS_STATUSES:
        return True, job_status
    if job_status in RAYJOB_FAILURE_STATUSES:
        message = str((status or {}).get("message") or job_status)
        return False, message
    return None, ""


__all__ = [
    "RAYJOB_FAILURE_STATUSES",
    "RAYJOB_SUCCESS_STATUSES",
    "rayjob_pod_specs",
    "rayjob_terminal_status",
    "rewrite_rayjob_for_lineage",
]
