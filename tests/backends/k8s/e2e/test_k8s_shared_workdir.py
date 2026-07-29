"""Shared-workdir e2e: co-located wrapped containers must not share state.

Regression for in-pod state isolation: two wrapped containers sharing a
workdir volume used to share `<workdir>/.roar/roar.db` (and the fixed
object-io events path), racing sqlite and cross-attributing lineage.
roar state now lives in a pod-local directory keyed by
pod_uid:container:completion_index:restart_attempt, so each container's
fragment carries only its own writes while the shared volume stays a
plain data surface.
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

SHARED_WORKDIR_MANIFEST = """\
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
      volumes:
        - name: work
          emptyDir: {{}}
      containers:
        - name: trainer-a
          image: python:3.12-slim
          workingDir: /work
          volumeMounts:
            - name: work
              mountPath: /work
          command:
            - python
            - -c
            - "open('out-a.bin', 'wb').write(b'alpha' * 8)"
        - name: trainer-b
          image: python:3.12-slim
          workingDir: /work
          volumeMounts:
            - name: work
              mountPath: /work
          command:
            - python
            - -c
            - "open('out-b.bin', 'wb').write(b'bravo' * 8)"
"""


@pytest.fixture(scope="module")
def shared_workdir_run(
    k8s_cluster: None,
    glaas_health: str,
    wheel_server: dict[str, str],  # noqa: F811
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, Any]:
    project_dir = tmp_path_factory.mktemp("k8s-shared-workdir")
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
                "wait_for_completion = true",
                "wait_timeout_seconds = 300",
                "poll_interval_seconds = 2.0",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    job_name = f"roar-shared-{uuid.uuid4().hex[:6]}"
    (project_dir / "job.yaml").write_text(
        SHARED_WORKDIR_MANIFEST.format(job_name=job_name, namespace=NAMESPACE),
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
            [
                "logs",
                "-n",
                NAMESPACE,
                "-l",
                f"job-name={job_name}",
                "--all-containers",
                "--tail=100",
            ],
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


def _query(run: dict[str, Any], sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    db_path = Path(run["project_dir"]) / ".roar" / "roar.db"
    assert db_path.is_file(), f"no roar.db produced\n{_describe(run)}"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def test_both_containers_reconstitute_as_distinct_tasks(
    shared_workdir_run: dict[str, Any],
) -> None:
    assert shared_workdir_run["exit_code"] == 0, _describe(shared_workdir_run)

    tasks = _query(
        shared_workdir_run,
        "SELECT id, command FROM jobs WHERE job_type = 'k8s_task' ORDER BY command",
    )
    assert len(tasks) == 2, _describe(shared_workdir_run)
    # The recorded command is k8s_task:<job>/<container> — one per container.
    assert tasks[0]["command"].endswith("/trainer-a"), _describe(shared_workdir_run)
    assert tasks[1]["command"].endswith("/trainer-b"), _describe(shared_workdir_run)


def test_outputs_are_attributed_to_their_own_container(
    shared_workdir_run: dict[str, Any],
) -> None:
    tasks = _query(
        shared_workdir_run,
        "SELECT id, command FROM jobs WHERE job_type = 'k8s_task' ORDER BY command",
    )
    assert len(tasks) == 2, _describe(shared_workdir_run)

    outputs_by_container: dict[str, set[str]] = {}
    for task in tasks:
        container = "trainer-a" if task["command"].endswith("/trainer-a") else "trainer-b"
        rows = _query(
            shared_workdir_run,
            "SELECT path FROM job_outputs WHERE job_id = ?",
            (task["id"],),
        )
        outputs_by_container[container] = {str(row["path"]) for row in rows}

    a_paths = {p for p in outputs_by_container["trainer-a"] if p.endswith(".bin")}
    b_paths = {p for p in outputs_by_container["trainer-b"] if p.endswith(".bin")}
    assert any(p.endswith("out-a.bin") for p in a_paths), _describe(shared_workdir_run)
    assert any(p.endswith("out-b.bin") for p in b_paths), _describe(shared_workdir_run)
    # The cross-attribution bug: with a shared <workdir>/.roar both tasks
    # exported from one db and each claimed the other's writes.
    assert not any(p.endswith("out-b.bin") for p in a_paths), _describe(shared_workdir_run)
    assert not any(p.endswith("out-a.bin") for p in b_paths), _describe(shared_workdir_run)
