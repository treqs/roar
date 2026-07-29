from __future__ import annotations

import copy
from typing import Any

import pytest

from roar.backends.k8s.manifest import (
    K8sManifestError,
    rewrite_manifest_for_lineage,
)

from .conftest import SINGLE_JOB_MANIFEST


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


def _job_container(rewrite) -> dict[str, Any]:
    job = next(doc for doc in rewrite.documents if doc.get("kind") == "Job")
    return job["spec"]["template"]["spec"]["containers"][0]


def test_wraps_command_preserving_original_as_positional_args() -> None:
    rewrite = _rewrite([copy.deepcopy(SINGLE_JOB_MANIFEST)])
    container = _job_container(rewrite)

    assert container["command"][:2] == ["/bin/sh", "-c"]
    script = container["command"][2]
    assert "python3 -m pip install --quiet roar-cli==0.3.7" in script
    assert "roar.backends.k8s.pod_entry" in script
    assert "run_fallback" in script
    # argv0 sentinel then the original command + args, args key removed
    assert container["command"][3:] == ["roar-k8s", "python", "train.py", "--epochs", "3"]
    assert "args" not in container
    assert rewrite.wrapped_containers == ["trainer"]


def test_injects_env_contract_without_clobbering_user_env() -> None:
    rewrite = _rewrite([copy.deepcopy(SINGLE_JOB_MANIFEST)])
    env = {entry["name"]: entry for entry in _job_container(rewrite)["env"]}

    assert env["USER_SETTING"]["value"] == "keep"
    assert env["ROAR_EXECUTION_BACKEND"]["value"] == "k8s"
    assert env["GLAAS_URL"]["value"] == "http://glaas:3001"
    assert env["ROAR_K8S_PARENT_JOB_UID"]["value"] == "cafe0123"
    assert env["ROAR_K8S_TASK_NAME"]["value"] == "train-demo/trainer"
    assert env["ROAR_SESSION_ID"]["valueFrom"]["secretKeyRef"] == {
        "name": "roar-fragment-abc",
        "key": "session_id",
    }
    assert env["ROAR_FRAGMENT_TOKEN"]["valueFrom"]["secretKeyRef"]["key"] == "token"
    assert env["ROAR_K8S_POD_UID"]["valueFrom"]["fieldRef"]["fieldPath"] == "metadata.uid"


def test_user_env_wins_over_injected_contract() -> None:
    manifest = copy.deepcopy(SINGLE_JOB_MANIFEST)
    manifest["spec"]["template"]["spec"]["containers"][0]["env"].append(
        {"name": "GLAAS_URL", "value": "http://user-override:9999"}
    )
    rewrite = _rewrite([manifest])
    env_entries = [
        entry for entry in _job_container(rewrite)["env"] if entry["name"] == "GLAAS_URL"
    ]
    assert env_entries == [{"name": "GLAAS_URL", "value": "http://user-override:9999"}]


def test_appends_secret_document_with_token() -> None:
    rewrite = _rewrite([copy.deepcopy(SINGLE_JOB_MANIFEST)])
    secret = next(doc for doc in rewrite.documents if doc.get("kind") == "Secret")

    assert secret["metadata"]["name"] == "roar-fragment-abc"
    assert secret["metadata"]["namespace"] == "ml"
    assert secret["stringData"]["session_id"] == "session-1"
    assert secret["stringData"]["token"] == "ff" * 32
    assert rewrite.namespace == "ml"


def test_prepare_mode_omits_secret_document() -> None:
    rewrite = _rewrite(
        [copy.deepcopy(SINGLE_JOB_MANIFEST)],
        session_id=None,
        fragment_token=None,
    )
    assert not any(doc.get("kind") == "Secret" for doc in rewrite.documents)
    env = {entry["name"]: entry for entry in _job_container(rewrite)["env"]}
    assert env["ROAR_SESSION_ID"]["valueFrom"]["secretKeyRef"]["name"] == "roar-fragment-abc"


def test_container_without_command_fails_actionably() -> None:
    manifest = copy.deepcopy(SINGLE_JOB_MANIFEST)
    del manifest["spec"]["template"]["spec"]["containers"][0]["command"]

    with pytest.raises(K8sManifestError, match="explicit command"):
        _rewrite([manifest])


def test_sidecar_without_command_is_skipped_not_fatal() -> None:
    manifest = copy.deepcopy(SINGLE_JOB_MANIFEST)
    manifest["spec"]["template"]["spec"]["containers"].append(
        {"name": "log-sidecar", "image": "busybox"}
    )
    rewrite = _rewrite([manifest])
    assert rewrite.wrapped_containers == ["trainer"]
    assert rewrite.skipped_containers == ["log-sidecar"]


def test_multiple_jobs_rejected() -> None:
    first = copy.deepcopy(SINGLE_JOB_MANIFEST)
    second = copy.deepcopy(SINGLE_JOB_MANIFEST)
    second["metadata"]["name"] = "train-demo-2"

    with pytest.raises(K8sManifestError, match="exactly one workload"):
        _rewrite([first, second])


def test_generate_name_rejected_with_hint() -> None:
    manifest = copy.deepcopy(SINGLE_JOB_MANIFEST)
    del manifest["metadata"]["name"]
    manifest["metadata"]["generateName"] = "train-"

    with pytest.raises(K8sManifestError, match="generateName"):
        _rewrite([manifest])


def test_non_job_documents_pass_through_unchanged() -> None:
    config_map = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "settings"},
        "data": {"key": "value"},
    }
    rewrite = _rewrite([copy.deepcopy(config_map), copy.deepcopy(SINGLE_JOB_MANIFEST)])
    passed_through = next(doc for doc in rewrite.documents if doc.get("kind") == "ConfigMap")
    assert passed_through == config_map


def test_namespace_override_wins() -> None:
    rewrite = _rewrite([copy.deepcopy(SINGLE_JOB_MANIFEST)], namespace_override="flagged")
    assert rewrite.namespace == "flagged"
