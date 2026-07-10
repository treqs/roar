from __future__ import annotations

from pathlib import Path

import pytest

from roar.execution.framework.planning import plan_execution_command
from roar.execution.framework.registry import get_execution_backend


def test_planner_routes_kubectl_apply_to_k8s_backend(
    job_manifest_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Keep planning offline: glaas.url falls back to the hosted default, and
    # an instrumented plan would otherwise register a session over the network.
    monkeypatch.setattr("roar.backends.k8s.submit._resolve_glaas_url", lambda: None)
    config_path = job_manifest_path.parent / ".roar" / "config.toml"
    config_path.write_text("[k8s]\nenabled = true\n", encoding="utf-8")

    plan = plan_execution_command(["kubectl", "apply", "-f", str(job_manifest_path)])
    assert plan.backend_name == "k8s"
    assert plan.execution_role == "submit"


def test_planner_falls_back_to_local_when_disabled(
    job_manifest_path: Path,
) -> None:
    config_path = job_manifest_path.parent / ".roar" / "config.toml"
    config_path.write_text("[k8s]\nenabled = false\n", encoding="utf-8")

    plan = plan_execution_command(["kubectl", "apply", "-f", str(job_manifest_path)])
    assert plan.backend_name == "local"


def test_k8s_backend_registered_with_expected_adapters() -> None:
    backend = get_execution_backend("k8s")
    assert backend.priority == 95
    assert backend.distributed is not None
    assert backend.distributed.fragment_reconstitution is not None
    assert backend.config is not None
    assert backend.config.section_name == "k8s"
    assert backend.policy is not None
    assert "ROAR_K8S_PARENT_JOB_UID" in backend.policy.job_environment_markers


def test_pod_entry_task_identity_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    from roar.backends.k8s.pod_entry import task_identity_from_environment

    environ = {
        "ROAR_K8S_POD_UID": "abc-123",
        "ROAR_K8S_CONTAINER": "trainer",
        "JOB_COMPLETION_INDEX": "2",
        "TORCHELASTIC_RESTART_COUNT": "1",
        "ROAR_K8S_TASK_NAME": "train-demo/trainer",
    }
    task_id, task_name = task_identity_from_environment(environ)
    assert task_id == "abc-123:trainer:2:1"
    assert task_name == "train-demo/trainer"

    task_id, task_name = task_identity_from_environment({})
    assert task_id == "unknown-pod:main:0:0"
    assert task_name == "k8s-task"
