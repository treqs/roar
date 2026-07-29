from __future__ import annotations

import copy
from typing import Any

from roar.backends.k8s.manifest import rewrite_manifest_for_lineage

from .conftest import SINGLE_JOB_MANIFEST


def _rewrite_image_mode(documents: list[dict[str, Any]], **overrides: Any):
    kwargs: dict[str, Any] = {
        "secret_name": "roar-fragment-abc",
        "session_id": "session-1",
        "fragment_token": "ff" * 32,
        "requirement": "roar-cli==0.3.7",
        "cluster_glaas_url": "http://glaas:3001",
        "tracer": "preload",
        "parent_job_uid": "cafe0123",
        "runtime_source": "image",
        "runtime_image": "roar-runtime:dev",
    }
    kwargs.update(overrides)
    return rewrite_manifest_for_lineage(documents, **kwargs)


def _job_pod_spec(rewrite) -> dict[str, Any]:
    job = next(doc for doc in rewrite.documents if doc.get("kind") == "Job")
    return job["spec"]["template"]["spec"]


def test_image_mode_adds_staging_init_container_and_volume() -> None:
    rewrite = _rewrite_image_mode([copy.deepcopy(SINGLE_JOB_MANIFEST)])
    pod_spec = _job_pod_spec(rewrite)

    volumes = {volume["name"] for volume in pod_spec["volumes"]}
    assert "roar-runtime" in volumes

    init = pod_spec["initContainers"][0]
    assert init["name"] == "roar-runtime-staging"
    assert init["image"] == "roar-runtime:dev"
    assert "/opt/roar-runtime/." in init["command"][2]

    container = pod_spec["containers"][0]
    mounts = {mount["name"]: mount for mount in container["volumeMounts"]}
    assert mounts["roar-runtime"]["mountPath"] == "/roar-runtime"
    assert mounts["roar-runtime"]["readOnly"] is True


def test_image_mode_wrapper_uses_pythonpath_not_pip() -> None:
    rewrite = _rewrite_image_mode([copy.deepcopy(SINGLE_JOB_MANIFEST)])
    script = _job_pod_spec(rewrite)["containers"][0]["command"][2]

    assert "pip install" not in script
    assert 'RT="/roar-runtime/cp' in script
    assert "PYTHONPATH" in script
    assert "roar.backends.k8s.pod_entry" in script
    assert "run_fallback" in script


def test_image_mode_is_idempotent_for_webhook_reinvocation() -> None:
    rewrite = _rewrite_image_mode([copy.deepcopy(SINGLE_JOB_MANIFEST)])
    job = next(doc for doc in rewrite.documents if doc.get("kind") == "Job")

    second = _rewrite_image_mode([copy.deepcopy(job)])
    pod_spec = _job_pod_spec(second)
    staging_inits = [
        container
        for container in pod_spec["initContainers"]
        if container["name"] == "roar-runtime-staging"
    ]
    staging_volumes = [volume for volume in pod_spec["volumes"] if volume["name"] == "roar-runtime"]
    assert len(staging_inits) == 1
    assert len(staging_volumes) == 1


def test_install_mode_untouched_by_new_fields() -> None:
    rewrite = _rewrite_image_mode(
        [copy.deepcopy(SINGLE_JOB_MANIFEST)],
        runtime_source="install",
        runtime_image="",
    )
    pod_spec = _job_pod_spec(rewrite)
    assert "initContainers" not in pod_spec
    script = pod_spec["containers"][0]["command"][2]
    assert "pip install --quiet roar-cli==0.3.7" in script
