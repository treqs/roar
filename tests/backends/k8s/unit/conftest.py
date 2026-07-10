from __future__ import annotations

from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped]

SINGLE_JOB_MANIFEST = {
    "apiVersion": "batch/v1",
    "kind": "Job",
    "metadata": {"name": "train-demo", "namespace": "ml"},
    "spec": {
        "backoffLimit": 0,
        "template": {
            "spec": {
                "restartPolicy": "Never",
                "containers": [
                    {
                        "name": "trainer",
                        "image": "python:3.12-slim",
                        "command": ["python", "train.py"],
                        "args": ["--epochs", "3"],
                        "env": [{"name": "USER_SETTING", "value": "keep"}],
                    }
                ],
            }
        },
    },
}


@pytest.fixture()
def k8s_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temp project with the k8s backend enabled and cwd pointed at it."""
    roar_dir = tmp_path / ".roar"
    roar_dir.mkdir()
    (roar_dir / "config.toml").write_text(
        '[k8s]\nenabled = true\n\n[glaas]\nurl = "http://localhost:3001"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ROAR_PROJECT_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture()
def job_manifest_path(k8s_project: Path) -> Path:
    manifest = k8s_project / "job.yaml"
    manifest.write_text(yaml.safe_dump(SINGLE_JOB_MANIFEST, sort_keys=False), encoding="utf-8")
    return manifest
