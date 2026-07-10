from __future__ import annotations

import copy
from typing import Any

import pytest

from roar.backends.k8s.manifest import (
    K8sManifestError,
    find_workload_documents,
    rewrite_manifest_for_lineage,
)

JOBSET_MANIFEST = {
    "apiVersion": "jobset.x-k8s.io/v1alpha2",
    "kind": "JobSet",
    "metadata": {"name": "dist-train", "namespace": "ml"},
    "spec": {
        "replicatedJobs": [
            {
                "name": "workers",
                "replicas": 1,
                "template": {
                    "spec": {
                        "completions": 2,
                        "parallelism": 2,
                        "completionMode": "Indexed",
                        "template": {
                            "spec": {
                                "restartPolicy": "Never",
                                "containers": [
                                    {
                                        "name": "trainer",
                                        "image": "pytorch/pytorch:latest",
                                        "command": ["torchrun", "train.py"],
                                        "args": ["--epochs", "3"],
                                    }
                                ],
                            }
                        },
                    }
                },
            }
        ]
    },
}

PYTORCHJOB_MANIFEST = {
    "apiVersion": "kubeflow.org/v1",
    "kind": "PyTorchJob",
    "metadata": {"name": "pt-train"},
    "spec": {
        "pytorchReplicaSpecs": {
            "Master": {
                "replicas": 1,
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "pytorch",
                                "image": "pytorch/pytorch:latest",
                                "command": ["python", "train.py"],
                            }
                        ]
                    }
                },
            },
            "Worker": {
                "replicas": 2,
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "pytorch",
                                "image": "pytorch/pytorch:latest",
                                "command": ["python", "train.py"],
                            }
                        ]
                    }
                },
            },
        }
    },
}

TRAINJOB_MANIFEST = {
    "apiVersion": "trainer.kubeflow.org/v1alpha1",
    "kind": "TrainJob",
    "metadata": {"name": "tj-train", "namespace": "ml"},
    "spec": {
        "runtimeRef": {"name": "torch-distributed"},
        "trainer": {
            "numNodes": 2,
            "command": ["torchrun", "train.py"],
            "args": ["--epochs", "3"],
            "env": [{"name": "USER_SETTING", "value": "keep"}],
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


def test_find_workload_documents_recognizes_all_kinds() -> None:
    documents = [
        copy.deepcopy(JOBSET_MANIFEST),
        copy.deepcopy(PYTORCHJOB_MANIFEST),
        copy.deepcopy(TRAINJOB_MANIFEST),
    ]
    found = find_workload_documents(documents)
    assert [workload.kind for _doc, workload in found] == ["JobSet", "PyTorchJob", "TrainJob"]


def test_jobset_rewrite_wraps_nested_pod_template() -> None:
    rewrite = _rewrite([copy.deepcopy(JOBSET_MANIFEST)])

    assert rewrite.workload_kind == "JobSet"
    assert rewrite.kubectl_resource == "jobsets.jobset.x-k8s.io"
    assert rewrite.job_name == "dist-train"
    assert rewrite.wrapped_containers == ["workers/trainer"]

    jobset = next(doc for doc in rewrite.documents if doc.get("kind") == "JobSet")
    container = jobset["spec"]["replicatedJobs"][0]["template"]["spec"]["template"]["spec"][
        "containers"
    ][0]
    assert container["command"][:2] == ["/bin/sh", "-c"]
    assert container["command"][3:] == ["roar-k8s", "torchrun", "train.py", "--epochs", "3"]
    assert "args" not in container

    env = {entry["name"]: entry for entry in container["env"]}
    assert env["ROAR_K8S_TASK_NAME"]["value"] == "dist-train/workers/trainer"
    assert env["ROAR_SESSION_ID"]["valueFrom"]["secretKeyRef"]["name"] == "roar-fragment-abc"

    secret = next(doc for doc in rewrite.documents if doc.get("kind") == "Secret")
    assert secret["metadata"]["namespace"] == "ml"


def test_pytorchjob_rewrite_wraps_all_replica_roles() -> None:
    rewrite = _rewrite([copy.deepcopy(PYTORCHJOB_MANIFEST)])

    assert rewrite.workload_kind == "PyTorchJob"
    assert rewrite.kubectl_resource == "pytorchjobs.kubeflow.org"
    assert sorted(rewrite.wrapped_containers) == ["Master/pytorch", "Worker/pytorch"]

    doc = next(d for d in rewrite.documents if d.get("kind") == "PyTorchJob")
    for role in ("Master", "Worker"):
        container = doc["spec"]["pytorchReplicaSpecs"][role]["template"]["spec"]["containers"][0]
        assert container["command"][:2] == ["/bin/sh", "-c"]
        env = {entry["name"]: entry for entry in container["env"]}
        assert env["ROAR_K8S_TASK_NAME"]["value"] == f"pt-train/{role}/pytorch"
        assert env["ROAR_K8S_PARENT_JOB_UID"]["value"] == "cafe0123"


def test_trainjob_rewrite_wraps_trainer_override() -> None:
    rewrite = _rewrite([copy.deepcopy(TRAINJOB_MANIFEST)])

    assert rewrite.workload_kind == "TrainJob"
    assert rewrite.kubectl_resource == "trainjobs.trainer.kubeflow.org"
    assert rewrite.wrapped_containers == ["node"]

    doc = next(d for d in rewrite.documents if d.get("kind") == "TrainJob")
    trainer = doc["spec"]["trainer"]
    assert trainer["command"][:2] == ["/bin/sh", "-c"]
    assert trainer["command"][3:] == ["roar-k8s", "torchrun", "train.py", "--epochs", "3"]
    assert "args" not in trainer

    env = {entry["name"]: entry for entry in trainer["env"]}
    assert env["USER_SETTING"]["value"] == "keep"
    assert env["ROAR_K8S_CONTAINER"]["value"] == "node"
    assert env["ROAR_K8S_TASK_NAME"]["value"] == "tj-train/node"


def test_trainjob_without_command_fails_actionably() -> None:
    manifest = copy.deepcopy(TRAINJOB_MANIFEST)
    del manifest["spec"]["trainer"]["command"]

    with pytest.raises(K8sManifestError, match=r"spec\.trainer\.command"):
        _rewrite([manifest])


def test_multiple_workload_kinds_rejected() -> None:
    with pytest.raises(K8sManifestError, match="exactly one workload"):
        _rewrite([copy.deepcopy(JOBSET_MANIFEST), copy.deepcopy(PYTORCHJOB_MANIFEST)])
