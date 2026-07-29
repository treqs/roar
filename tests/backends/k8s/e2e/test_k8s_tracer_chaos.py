"""Tracer-chaos e2e: a roar setup failure must never block training.

Regression for the workload-safety contract: `k8s.tracer = "ebpf"` is a
valid tracer mode, but inside an unprivileged pod the ebpf backend
cannot pass preflight (and the staged runtime ships no ebpf tracer), so
in-pod `roar run --tracer ebpf` fails before launching the workload.
pod_entry must detect the pre-launch failure through the run report
(ROAR_RUN_REPORT_FILE) and rerun the original command uninstrumented —
the Job completes with the workload's own exit code and no lineage,
rather than failing training over a lineage problem.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from .conftest import KUBE_CONTEXT, NAMESPACE, TOOLS_BIN, kubectl
from .test_k8s_product_path import wheel_server  # noqa: F401

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.k8s_e2e,
    pytest.mark.timeout(900),
]

CHAOS_MANIFEST_TEMPLATE = """\
apiVersion: batch/v1
kind: Job
metadata:
  name: {job_name}
  namespace: {namespace}
  labels:
    app.kubernetes.io/part-of: roar-k8s-e2e
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 1800
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: trainer
          image: python:3.12-slim
          workingDir: /tmp
          command:
            - python
            - -c
            - "print('CHAOS_TRAIN_OK')"
"""


@pytest.fixture(scope="module")
def chaos_run(
    k8s_cluster: None,
    glaas_health: str,
    wheel_server: dict[str, str],  # noqa: F811
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, Any]:
    project_dir = tmp_path_factory.mktemp("k8s-tracer-chaos")
    roar_dir = project_dir / ".roar"
    roar_dir.mkdir()
    (roar_dir / "config.toml").write_text(
        "\n".join(
            [
                "[glaas]",
                'url = "http://localhost:3001"',
                "",
                "[k8s]",
                "enabled = true",
                f'runtime_install_requirement = "{wheel_server["url"]}"',
                'cluster_glaas_url = "http://glaas:3001"',
                # Valid tracer mode that cannot work in an unprivileged
                # pod: forces an in-pod pre-launch setup failure.
                'tracer = "ebpf"',
                "wait_for_completion = true",
                "wait_timeout_seconds = 300",
                "poll_interval_seconds = 2.0",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    job_name = f"roar-chaos-{uuid.uuid4().hex[:6]}"
    manifest_path = project_dir / "job.yaml"
    manifest_path.write_text(
        CHAOS_MANIFEST_TEMPLATE.format(job_name=job_name, namespace=NAMESPACE),
        encoding="utf-8",
    )

    env = dict(os.environ)
    env["PATH"] = f"{TOOLS_BIN}{os.pathsep}{env.get('PATH', '')}"
    env.pop("GLAAS_URL", None)
    env.pop("ROAR_PROJECT_DIR", None)

    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "roar",
                "run",
                "kubectl",
                "apply",
                "--context",
                KUBE_CONTEXT,
                "-f",
                "job.yaml",
            ],
            cwd=project_dir,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=600,
        )
        pod_logs = kubectl(
            ["logs", "-n", NAMESPACE, "-l", f"job-name={job_name}", "--tail=100"],
            check=False,
        )
        return {
            "project_dir": project_dir,
            "job_name": job_name,
            "exit_code": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "pod_logs": pod_logs.stdout + pod_logs.stderr,
        }
    finally:
        kubectl(["delete", f"job/{job_name}", "-n", NAMESPACE, "--ignore-not-found"], check=False)
        kubectl(
            [
                "delete",
                "secret",
                "-n",
                NAMESPACE,
                "-l",
                "app.kubernetes.io/managed-by=roar",
                "--ignore-not-found",
            ],
            check=False,
        )


def _describe(run: dict[str, Any]) -> str:
    return (
        f"exit={run['exit_code']}\nstdout:\n{run['stdout']}\nstderr:\n{run['stderr']}\n"
        f"pod logs:\n{run.get('pod_logs', '')}"
    )


def test_workload_survives_tracer_setup_failure(chaos_run: dict[str, Any]) -> None:
    # The Job must complete even though roar's tracer cannot start: the
    # submit (which waits for completion) exits 0 on the workload's own
    # success.
    assert chaos_run["exit_code"] == 0, _describe(chaos_run)
    assert "CHAOS_TRAIN_OK" in chaos_run["pod_logs"], _describe(chaos_run)
    assert "running uninstrumented" in chaos_run["pod_logs"], _describe(chaos_run)


def test_no_lineage_is_fabricated_for_uninstrumented_run(chaos_run: dict[str, Any]) -> None:
    db_path = Path(chaos_run["project_dir"]) / ".roar" / "roar.db"
    assert db_path.is_file(), _describe(chaos_run)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        tasks = conn.execute("SELECT id FROM jobs WHERE job_type = 'k8s_task'").fetchall()
    finally:
        conn.close()
    assert tasks == [], _describe(chaos_run)
