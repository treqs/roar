"""`roar k8s prepare` must preview exactly what the managed path submits.

Regression for config drift: prepare used to pass only requirement,
GLaaS URL, tracer, Secret name, and parent UID to the rewriter, so an
image-staged, bundle-enabled, mount-mapped, or namespace-overridden
configuration previewed differently from what `roar run kubectl apply`
would actually apply.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped]
from click.testing import CliRunner

from roar.cli.commands.k8s import k8s

from .conftest import SINGLE_JOB_MANIFEST


@pytest.fixture()
def configured_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    roar_dir = tmp_path / ".roar"
    roar_dir.mkdir()
    (roar_dir / "config.toml").write_text(
        "\n".join(
            [
                "[k8s]",
                "enabled = true",
                'bundle_dir = "/mnt/bundles"',
                'runtime_source = "image"',
                'runtime_image = "roar-runtime:pinned"',
                "",
                "[k8s.mount_map]",
                '"/data" = "pvc://training-data"',
                "",
                "[glaas]",
                'url = "http://localhost:3001"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "job.yaml"
    manifest.write_text(yaml.safe_dump(SINGLE_JOB_MANIFEST, sort_keys=False), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ROAR_PROJECT_DIR", str(tmp_path))
    return tmp_path


def test_prepare_mirrors_managed_rewrite_configuration(configured_project: Path) -> None:
    output_path = configured_project / "prepared.yaml"
    result = CliRunner().invoke(
        k8s,
        [
            "prepare",
            "-f",
            "job.yaml",
            "-o",
            str(output_path),
            "-n",
            "override-ns",
        ],
    )
    assert result.exit_code == 0, result.output

    documents = list(yaml.safe_load_all(output_path.read_text(encoding="utf-8")))
    job = next(doc for doc in documents if doc and doc.get("kind") == "Job")
    pod_spec = job["spec"]["template"]["spec"]

    # namespace_override feeds the resolved namespace (Secret placement and
    # the recorded context), exactly as `kubectl apply -n` does on the
    # managed path; the workload's own metadata is never mutated.
    assert "(namespace override-ns)" in result.output

    # Image staging: the managed path would add the runtime-staging init
    # container with the pinned image; prepare must show the same.
    init_names = [c.get("name") for c in pod_spec.get("initContainers", [])]
    assert "roar-runtime-staging" in init_names, result.output

    env = {
        entry.get("name"): entry.get("value")
        for entry in pod_spec["containers"][0].get("env", [])
        if isinstance(entry, dict)
    }
    assert env.get("ROAR_K8S_BUNDLE_DIR") == "/mnt/bundles"
    assert "pvc://training-data" in str(env.get("ROAR_K8S_MOUNT_MAP"))


def test_prepare_warns_when_proxy_sidecar_cannot_inject(
    configured_project: Path,
) -> None:
    config_path = configured_project / ".roar" / "config.toml"
    config_path.write_text(
        '[k8s]\nenabled = true\nproxy_sidecar = true\n\n[glaas]\nurl = "http://localhost:3001"\n',
        encoding="utf-8",
    )
    output_path = configured_project / "prepared.yaml"
    result = CliRunner().invoke(
        k8s,
        ["prepare", "-f", "job.yaml", "-o", str(output_path)],
    )
    assert result.exit_code == 0, result.output
    assert "proxy_sidecar requires" in result.output
