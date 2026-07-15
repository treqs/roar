from __future__ import annotations

import copy
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]

from roar.backends.k8s.manifest import (
    K8sManifestError,
    rewrite_manifest_for_lineage,
    workload_kind_for_document,
)
from roar.backends.k8s.rayjob import rayjob_terminal_status

RAYJOB_MANIFEST = {
    "apiVersion": "ray.io/v1",
    "kind": "RayJob",
    "metadata": {"name": "ray-train", "namespace": "ml"},
    "spec": {
        "entrypoint": "python train.py --epochs 3",
        "runtimeEnvYAML": "pip:\n  - pandas\nenv_vars:\n  USER_VAR: keep\n",
        "rayClusterSpec": {
            "headGroupSpec": {
                "rayStartParams": {},
                "template": {
                    "spec": {"containers": [{"name": "ray-head", "image": "rayproject/ray:2.46.0"}]}
                },
            },
            "workerGroupSpecs": [
                {
                    "groupName": "workers",
                    "replicas": 1,
                    "template": {
                        "spec": {
                            "containers": [{"name": "ray-worker", "image": "rayproject/ray:2.46.0"}]
                        }
                    },
                }
            ],
        },
    },
}


def _rewrite(documents: list[dict[str, Any]], **overrides: Any):
    kwargs: dict[str, Any] = {
        "secret_name": "roar-fragment-abc",
        "session_id": "session-1",
        "fragment_token": "ff" * 32,
        "requirement": "roar-cli==0.3.7",
        "cluster_glaas_url": "http://glaas:3001",
        "tracer": "preload",
        "parent_job_uid": "cafe0123",
    }
    kwargs.update(overrides)
    return rewrite_manifest_for_lineage(documents, **kwargs)


def test_rayjob_recognized_as_workload() -> None:
    workload = workload_kind_for_document(RAYJOB_MANIFEST)
    assert workload is not None
    assert workload.kind == "RayJob"
    assert workload.kubectl_resource == "rayjobs.ray.io"
    assert workload.rewrite_style == "rayjob"


def test_rayjob_entrypoint_wrapped_through_ray_driver() -> None:
    rewrite = _rewrite([copy.deepcopy(RAYJOB_MANIFEST)])
    doc = next(d for d in rewrite.documents if d.get("kind") == "RayJob")

    assert doc["spec"]["entrypoint"] == (
        "python -m roar.execution.runtime.driver_entrypoint -- python train.py --epochs 3"
    )
    assert rewrite.workload_kind == "RayJob"
    assert "entrypoint" in rewrite.wrapped_containers


def test_rayjob_runtime_env_carries_ray_contract_without_secrets() -> None:
    rewrite = _rewrite([copy.deepcopy(RAYJOB_MANIFEST)])
    doc = next(d for d in rewrite.documents if d.get("kind") == "RayJob")
    runtime_env = yaml.safe_load(doc["spec"]["runtimeEnvYAML"])

    assert "roar-cli==0.3.7" in runtime_env["pip"]
    assert "pandas" in runtime_env["pip"]
    assert (
        runtime_env["worker_process_setup_hook"]
        == "roar.execution.runtime.worker_bootstrap.startup"
    )

    env_vars = runtime_env["env_vars"]
    assert env_vars["USER_VAR"] == "keep"
    assert env_vars["ROAR_EXECUTION_BACKEND"] == "ray"
    assert env_vars["ROAR_JOB_ID"] == "cafe0123"
    # Workers stamp this into each fragment's parent_job_uid; it is the DAG
    # edge from ray_task rows back to the recorded k8s submit job.
    assert env_vars["ROAR_DRIVER_JOB_UID"] == "cafe0123"
    assert env_vars["GLAAS_URL"] == "http://glaas:3001"
    assert env_vars["ROAR_RAY_NODE_AGENTS"] == "0"
    # Credentials must never appear inline in the CR.
    assert "ROAR_SESSION_ID" not in env_vars
    assert "ROAR_FRAGMENT_TOKEN" not in env_vars
    # No proxy runs in the pods: the local-proxy redirect must be stripped
    # or user S3 traffic would hit a dead localhost port.
    assert "AWS_ENDPOINT_URL" not in env_vars
    assert "ROAR_PROXY_PORT" not in env_vars
    # ROAR_WRAP stays off for RayJob v1 (pip-virtualenv .pth timing blows
    # the worker registration timeout); the setup hook captures instead.
    assert "ROAR_WRAP" not in env_vars


def test_rayjob_pods_get_secret_refs_and_secret_doc() -> None:
    rewrite = _rewrite([copy.deepcopy(RAYJOB_MANIFEST)])
    doc = next(d for d in rewrite.documents if d.get("kind") == "RayJob")

    cluster = doc["spec"]["rayClusterSpec"]
    pod_containers = [
        cluster["headGroupSpec"]["template"]["spec"]["containers"][0],
        cluster["workerGroupSpecs"][0]["template"]["spec"]["containers"][0],
    ]
    for container in pod_containers:
        env = {entry["name"]: entry for entry in container["env"]}
        assert env["ROAR_SESSION_ID"]["valueFrom"]["secretKeyRef"]["name"] == "roar-fragment-abc"
        assert env["ROAR_FRAGMENT_TOKEN"]["valueFrom"]["secretKeyRef"]["key"] == "token"

    secret = next(d for d in rewrite.documents if d.get("kind") == "Secret")
    assert secret["metadata"]["namespace"] == "ml"


def test_rayjob_preserves_pip_dict_options() -> None:
    manifest = copy.deepcopy(RAYJOB_MANIFEST)
    manifest["spec"]["runtimeEnvYAML"] = yaml.safe_dump(
        {
            "pip": {"packages": ["pandas"], "pip_check": False, "pip_version": "==23.3.1"},
            "env_vars": {"USER_VAR": "keep"},
        },
        sort_keys=False,
    )
    rewrite = _rewrite([manifest])
    doc = next(d for d in rewrite.documents if d.get("kind") == "RayJob")
    runtime_env = yaml.safe_load(doc["spec"]["runtimeEnvYAML"])

    pip = runtime_env["pip"]
    assert pip["pip_check"] is False
    assert pip["pip_version"] == "==23.3.1"
    assert pip["packages"] == ["pandas", "roar-cli==0.3.7"]


def test_rayjob_rejects_pip_requirements_file_reference() -> None:
    manifest = copy.deepcopy(RAYJOB_MANIFEST)
    manifest["spec"]["runtimeEnvYAML"] = "pip: requirements.txt\n"

    with pytest.raises(K8sManifestError, match="requirements"):
        _rewrite([manifest])


def test_rayjob_chains_user_worker_setup_hook() -> None:
    manifest = copy.deepcopy(RAYJOB_MANIFEST)
    manifest["spec"]["runtimeEnvYAML"] = yaml.safe_dump(
        {"worker_process_setup_hook": "my_pkg.hooks.setup"},
        sort_keys=False,
    )
    rewrite = _rewrite([manifest])
    doc = next(d for d in rewrite.documents if d.get("kind") == "RayJob")
    runtime_env = yaml.safe_load(doc["spec"]["runtimeEnvYAML"])

    assert (
        runtime_env["worker_process_setup_hook"]
        == "roar.execution.runtime.worker_bootstrap.startup"
    )
    # roar's startup hook invokes the displaced user hook after capture is
    # installed (worker_bootstrap.startup reads ROAR_USER_SETUP_HOOK).
    assert runtime_env["env_vars"]["ROAR_USER_SETUP_HOOK"] == "my_pkg.hooks.setup"


def test_rayjob_does_not_chain_roar_hook_to_itself() -> None:
    manifest = copy.deepcopy(RAYJOB_MANIFEST)
    manifest["spec"]["runtimeEnvYAML"] = yaml.safe_dump(
        {"worker_process_setup_hook": "roar.execution.runtime.worker_bootstrap.startup"},
        sort_keys=False,
    )
    rewrite = _rewrite([manifest])
    doc = next(d for d in rewrite.documents if d.get("kind") == "RayJob")
    runtime_env = yaml.safe_load(doc["spec"]["runtimeEnvYAML"])

    assert "ROAR_USER_SETUP_HOOK" not in runtime_env["env_vars"]


def test_rayjob_preserves_user_aws_endpoint_url() -> None:
    manifest = copy.deepcopy(RAYJOB_MANIFEST)
    manifest["spec"]["runtimeEnvYAML"] = yaml.safe_dump(
        {"env_vars": {"AWS_ENDPOINT_URL": "http://minio:9000"}},
        sort_keys=False,
    )
    rewrite = _rewrite([manifest])
    doc = next(d for d in rewrite.documents if d.get("kind") == "RayJob")
    env_vars = yaml.safe_load(doc["spec"]["runtimeEnvYAML"])["env_vars"]

    # Only roar's own local-proxy redirect is dropped (no proxy runs in the
    # pods); a user-supplied object-store endpoint must survive the rewrite.
    assert env_vars["AWS_ENDPOINT_URL"] == "http://minio:9000"
    assert "ROAR_PROXY_PORT" not in env_vars


def test_rayjob_without_entrypoint_fails_actionably() -> None:
    manifest = copy.deepcopy(RAYJOB_MANIFEST)
    del manifest["spec"]["entrypoint"]

    with pytest.raises(K8sManifestError, match="entrypoint"):
        _rewrite([manifest])


def test_rayjob_terminal_status_mapping() -> None:
    assert rayjob_terminal_status({"status": {"jobStatus": "SUCCEEDED"}}) == (True, "SUCCEEDED")
    succeeded, message = rayjob_terminal_status(
        {"status": {"jobStatus": "FAILED", "message": "boom"}}
    )
    assert succeeded is False and message == "boom"
    assert rayjob_terminal_status({"status": {"jobStatus": "RUNNING"}})[0] is None
    assert rayjob_terminal_status({})[0] is None


def test_rayjob_plan_delegates_finalizer_to_ray(
    job_manifest_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import roar.backends.k8s.submit as submit_module

    monkeypatch.setattr(
        submit_module,
        "_register_fragment_session",
        lambda *args, **kwargs: None,
    )
    rayjob_path = job_manifest_path.parent / "rayjob.yaml"
    rayjob_path.write_text(yaml.safe_dump(RAYJOB_MANIFEST, sort_keys=False), encoding="utf-8")

    plan = submit_module.plan_kubectl_job_submit_command(
        ["kubectl", "apply", "-f", str(rayjob_path)]
    )
    assert plan.backend_name == "k8s"
    assert plan.session_id is not None
    # RayJob reconstitution is delegated to the Ray backend explicitly, so
    # the framework must not attach the k8s finalizer.
    assert plan.finalize_run is not None
