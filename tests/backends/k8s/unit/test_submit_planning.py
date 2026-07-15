from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped]

from roar.backends.k8s.submit import (
    load_submit_context,
    matches_kubectl_job_submit_command,
    plan_kubectl_job_submit_command,
    resolve_runtime_requirement,
)


def test_matches_kubectl_apply_with_job_manifest(job_manifest_path: Path) -> None:
    assert matches_kubectl_job_submit_command(["kubectl", "apply", "-f", str(job_manifest_path)])
    assert matches_kubectl_job_submit_command(
        ["kubectl", "create", f"--filename={job_manifest_path}", "-n", "ml"]
    )


def test_matches_kubectl_with_global_flags_before_verb(job_manifest_path: Path) -> None:
    """Global flags routinely precede the verb; they must not bypass lineage."""
    assert matches_kubectl_job_submit_command(
        ["kubectl", "--context", "prod-cluster", "apply", "-f", str(job_manifest_path)]
    )
    assert matches_kubectl_job_submit_command(
        ["kubectl", "-n", "ml", "create", "-f", str(job_manifest_path)]
    )
    assert matches_kubectl_job_submit_command(
        [
            "kubectl",
            "--kubeconfig",
            "/tmp/kc",
            "--context",
            "c",
            "apply",
            "-f",
            str(job_manifest_path),
        ]
    )


def test_does_not_match_non_submit_verbs_with_manifest(job_manifest_path: Path) -> None:
    assert not matches_kubectl_job_submit_command(
        ["kubectl", "delete", "-f", str(job_manifest_path)]
    )
    assert not matches_kubectl_job_submit_command(["kubectl", "diff", "-f", str(job_manifest_path)])


def test_does_not_match_when_disabled(job_manifest_path: Path) -> None:
    config_path = job_manifest_path.parent / ".roar" / "config.toml"
    config_path.write_text("[k8s]\nenabled = false\n", encoding="utf-8")
    assert not matches_kubectl_job_submit_command(
        ["kubectl", "apply", "-f", str(job_manifest_path)]
    )


def test_does_not_match_non_job_manifests(k8s_project: Path) -> None:
    manifest = k8s_project / "deploy.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "web"},
            }
        ),
        encoding="utf-8",
    )
    assert not matches_kubectl_job_submit_command(["kubectl", "apply", "-f", str(manifest)])


def test_does_not_match_other_kubectl_commands(job_manifest_path: Path) -> None:
    assert not matches_kubectl_job_submit_command(["kubectl", "get", "pods", "-A"])
    assert not matches_kubectl_job_submit_command(
        ["kubectl", "delete", "-f", str(job_manifest_path)]
    )


def test_plan_without_glaas_returns_original_command(
    job_manifest_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # glaas.url has a hosted default, so "unconfigured" is modeled at the
    # resolver seam rather than via env/config manipulation.
    monkeypatch.setattr("roar.backends.k8s.submit._resolve_glaas_url", lambda: None)

    command = ["kubectl", "apply", "-f", str(job_manifest_path)]
    plan = plan_kubectl_job_submit_command(command)

    assert plan.backend_name == "k8s"
    assert plan.command == command
    assert plan.session_id is None
    assert plan.execution_role == "submit"


def test_plan_rewrites_manifest_and_persists_context(
    job_manifest_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registered: dict[str, str] = {}

    def fake_register(glaas_url: str, session_id: str, token_hash: str, ttl: int = 86400) -> None:
        registered.update(
            {"glaas_url": glaas_url, "session_id": session_id, "token_hash": token_hash}
        )

    monkeypatch.setattr(
        "roar.backends.k8s.submit._register_fragment_session",
        fake_register,
    )

    command = ["kubectl", "apply", "--context", "kind-test", "-f", str(job_manifest_path)]
    plan = plan_kubectl_job_submit_command(command)

    assert plan.backend_name == "k8s"
    assert plan.session_id == registered["session_id"]
    assert registered["glaas_url"] == "http://localhost:3001"

    prepared_path = Path(plan.command[plan.command.index("-f") + 1])
    assert prepared_path.is_file()
    assert prepared_path.parent == job_manifest_path.parent / ".roar" / "k8s" / "prepared"
    # Context flags are preserved verbatim.
    assert plan.command[:4] == ["kubectl", "apply", "--context", "kind-test"]

    documents = list(yaml.safe_load_all(prepared_path.read_text(encoding="utf-8")))
    kinds = [doc.get("kind") for doc in documents if doc]
    assert kinds.count("Secret") == 1
    assert kinds.count("Job") == 1

    context = load_submit_context(prepared_path)
    assert context is not None
    assert context.original_command == command
    assert context.job_name == "train-demo"
    assert context.namespace == "ml"
    assert context.session_id == plan.session_id
    assert len(context.parent_job_uid) == 8
    assert context.wrapped_containers == ["trainer"]

    key_path = job_manifest_path.parent / ".roar" / "fragment-sessions" / f"{plan.session_id}.key"
    assert key_path.is_file()
    key_payload = json.loads(key_path.read_text(encoding="utf-8"))
    assert key_payload["session_id"] == plan.session_id


def test_plan_from_subdirectory_saves_state_in_project_root(
    job_manifest_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plan-time state must land in the .roar the finalizer will read."""
    project_root = job_manifest_path.parent
    # Real projects are git repos; config discovery bounds its upward walk
    # by the repo root, so the fixture needs one to search past the subdir.
    subprocess.run(["git", "init", "-q"], cwd=project_root, check=True)
    nested = project_root / "jobs" / "nested"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    monkeypatch.delenv("ROAR_PROJECT_DIR", raising=False)
    monkeypatch.setattr(
        "roar.backends.k8s.submit._register_fragment_session",
        lambda *args, **kwargs: None,
    )

    plan = plan_kubectl_job_submit_command(["kubectl", "apply", "-f", str(job_manifest_path)])

    assert plan.session_id
    key_path = project_root / ".roar" / "fragment-sessions" / f"{plan.session_id}.key"
    assert key_path.is_file()
    prepared_path = Path(plan.command[plan.command.index("-f") + 1])
    assert prepared_path.parent == project_root / ".roar" / "k8s" / "prepared"
    assert not (nested / ".roar").exists()


def test_plan_degrades_to_uninstrumented_when_registration_fails(
    job_manifest_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_register(*args: object, **kwargs: object) -> None:
        raise RuntimeError("glaas is down")

    monkeypatch.setattr(
        "roar.backends.k8s.submit._register_fragment_session",
        failing_register,
    )

    command = ["kubectl", "apply", "-f", str(job_manifest_path)]
    plan = plan_kubectl_job_submit_command(command)

    assert plan.command == command
    assert plan.session_id is None


def test_resolve_runtime_requirement_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROAR_CLUSTER_PIP_REQ", "http://host/wheel.whl")
    assert (
        resolve_runtime_requirement({"runtime_install_requirement": "roar-cli==1.0"})
        == "http://host/wheel.whl"
    )

    monkeypatch.delenv("ROAR_CLUSTER_PIP_REQ")
    assert (
        resolve_runtime_requirement({"runtime_install_requirement": "roar-cli==1.0"})
        == "roar-cli==1.0"
    )
    assert resolve_runtime_requirement({}).startswith("roar-cli")
