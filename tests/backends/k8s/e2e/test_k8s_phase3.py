"""Phase-3 e2e: image-staged runtime (and, below, the webhook injector).

Requires the roar-runtime image loaded into the cluster:

    bash scripts/build_runtime_image.sh
    .tools/bin/kind load docker-image roar-runtime:dev --name roar-k8s-e2e
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

import pytest

from .conftest import KUBE_CONTEXT, NAMESPACE, kubectl
from .test_k8s_distributed import (
    SINGLE_JOB_TEMPLATE,
    _cleanup,
    _describe,
    _pod_logs,
    _submit,
)
from .test_k8s_product_path import wheel_server  # noqa: F401  (module fixture reuse)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.k8s_e2e,
    pytest.mark.timeout(900),
]

RUNTIME_IMAGE = "roar-runtime:dev"


def _query(project_dir: Path, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    conn = sqlite3.connect(project_dir / ".roar" / "roar.db")
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _runtime_image_loaded() -> bool:
    import subprocess

    result = subprocess.run(
        ["docker", "exec", "roar-k8s-e2e-worker", "crictl", "inspecti", "-q",
         f"docker.io/library/{RUNTIME_IMAGE}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def _write_image_mode_project(project_dir: Path) -> None:
    roar_dir = project_dir / ".roar"
    roar_dir.mkdir(parents=True, exist_ok=True)
    (roar_dir / "config.toml").write_text(
        "\n".join(
            [
                "[glaas]",
                'url = "http://localhost:3001"',
                "",
                "[k8s]",
                "enabled = true",
                'runtime_source = "image"',
                f'runtime_image = "{RUNTIME_IMAGE}"',
                'cluster_glaas_url = "http://glaas:3001"',
                "wait_for_completion = true",
                "wait_timeout_seconds = 420",
                "poll_interval_seconds = 2.0",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def test_image_staged_runtime_captures_without_network_install(
    k8s_cluster: None,
    glaas_health: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    if not _runtime_image_loaded():
        pytest.skip(
            "roar-runtime:dev not loaded; run scripts/build_runtime_image.sh "
            "and kind load docker-image roar-runtime:dev --name roar-k8s-e2e"
        )

    project_dir = tmp_path_factory.mktemp("k8s-image-mode")
    _write_image_mode_project(project_dir)

    job_name = f"roar-img-{uuid.uuid4().hex[:6]}"
    (project_dir / "job.yaml").write_text(
        SINGLE_JOB_TEMPLATE.format(name=job_name, namespace=NAMESPACE),
        encoding="utf-8",
    )

    try:
        completed = _submit("job.yaml", cwd=project_dir)
        logs = _pod_logs(f"job-name={job_name}")
        run = {
            "exit_code": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "pod_logs": logs,
        }
        assert completed.returncode == 0, _describe(run)

        # The submitted Job carries the staging init container.
        job_doc = json.loads(
            kubectl(
                ["get", f"job/{job_name}", "-n", NAMESPACE, "-o", "json"]
            ).stdout
        )
        pod_spec = job_doc["spec"]["template"]["spec"]
        init_names = [c["name"] for c in pod_spec.get("initContainers", [])]
        assert "roar-runtime-staging" in init_names, init_names
        assert not any(
            "pip install" in part
            for container in pod_spec["containers"]
            for part in container.get("command", [])
        ), "image mode must not pip install in the wrapper"

        tasks = _query(project_dir, "SELECT id FROM jobs WHERE job_type = 'k8s_task'")
        assert len(tasks) == 1, _describe(run)
        outputs = {
            str(row["path"])
            for row in _query(
                project_dir,
                "SELECT path FROM job_outputs WHERE job_id = ?",
                (tasks[0]["id"],),
            )
        }
        assert any(path.endswith("summary.json") for path in outputs), (
            f"{outputs}\n{_describe(run)}"
        )
    finally:
        _cleanup(job_name)
