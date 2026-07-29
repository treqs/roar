"""Live operator validation: PyTorchJob (training-operator v1) and TrainJob (trainer v2).

Requires ``bootstrap_k8s.sh --with-kubeflow``; tests skip when the CRDs are
absent. Workloads use slim Python images — the operators' pod/env wiring is
what's under test (per-role identity, RANK/PET_NODE_RANK index chains, real
terminal conditions), not torch itself. The TrainJob test clones the shipped
``torch-distributed`` ClusterTrainingRuntime with a slim image so the real
TrainJob -> JobSet -> pod pipeline runs without multi-GB pulls.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

import pytest

from .conftest import NAMESPACE, kubectl
from .test_k8s_distributed import (
    PINNED_NODE,
    _cleanup,
    _describe,
    _indent_script,
    _pod_logs,
    _submit,
    _write_project,
)
from .test_k8s_product_path import wheel_server  # noqa: F401  (module fixture reuse)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.k8s_e2e,
    pytest.mark.timeout(900),
]

PT_WORKER_SCRIPT = """\
import json
import os
import time
from pathlib import Path

rank = os.environ.get("RANK") or os.environ.get("PET_NODE_RANK") or "0"
Path(f"out-rank-{rank}.json").write_text(json.dumps({"rank": rank}))
if rank == "0":
    deadline = time.time() + 180
    while not Path("/state/worker-done").exists():
        if time.time() > deadline:
            raise SystemExit("timed out waiting for worker")
        time.sleep(2)
    time.sleep(2)
else:
    Path("/state/worker-done").write_text("done")
print(f"pytorch replica rank {rank} done")
"""

PYTORCHJOB_TEMPLATE = """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: {name}-worker
  namespace: {namespace}
data:
  pt_worker.py: |
{worker_script}
---
apiVersion: kubeflow.org/v1
kind: PyTorchJob
metadata:
  name: {name}
  namespace: {namespace}
  labels:
    app.kubernetes.io/part-of: roar-k8s-e2e
spec:
  pytorchReplicaSpecs:
    Master:
      replicas: 1
      restartPolicy: Never
      template:
        spec:
          nodeSelector:
            kubernetes.io/hostname: {pinned_node}
          volumes:
            - name: work
              emptyDir: {{}}
            - name: state
              hostPath:
                path: /var/tmp/roar-e2e-pt-{name}
                type: DirectoryOrCreate
            - name: workload
              configMap:
                name: {name}-worker
          containers:
            - name: pytorch
              image: python:3.12-slim
              workingDir: /work
              volumeMounts:
                - name: work
                  mountPath: /work
                - name: state
                  mountPath: /state
                - name: workload
                  mountPath: /workload
                  readOnly: true
              command: ["python", "/workload/pt_worker.py"]
    Worker:
      replicas: 1
      restartPolicy: Never
      template:
        spec:
          nodeSelector:
            kubernetes.io/hostname: {pinned_node}
          volumes:
            - name: work
              emptyDir: {{}}
            - name: state
              hostPath:
                path: /var/tmp/roar-e2e-pt-{name}
                type: DirectoryOrCreate
            - name: workload
              configMap:
                name: {name}-worker
          containers:
            - name: pytorch
              image: python:3.12-slim
              workingDir: /work
              volumeMounts:
                - name: work
                  mountPath: /work
                - name: state
                  mountPath: /state
                - name: workload
                  mountPath: /workload
                  readOnly: true
              command: ["python", "/workload/pt_worker.py"]
"""

TRAINJOB_TEMPLATE = """\
apiVersion: trainer.kubeflow.org/v1alpha1
kind: TrainJob
metadata:
  name: {name}
  namespace: {namespace}
  labels:
    app.kubernetes.io/part-of: roar-k8s-e2e
spec:
  runtimeRef:
    name: {runtime}
  trainer:
    numNodes: 2
    command:
      - python
      - -c
      - >-
        import json, os;
        rank = os.environ.get('PET_NODE_RANK') or os.environ.get('JOB_COMPLETION_INDEX') or '0';
        open('node-' + rank + '.json', 'w').write(json.dumps({{'rank': rank}}));
        open('model.bin', 'wb').write(rank.encode() * 8)
"""


def _query(project_dir: Path, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    conn = sqlite3.connect(project_dir / ".roar" / "roar.db")
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _crd_available(crd: str) -> bool:
    return kubectl(["get", "crd", crd], check=False).returncode == 0


def _task_rows(project_dir: Path) -> list[dict[str, Any]]:
    rows = _query(
        project_dir,
        "SELECT id, metadata FROM jobs WHERE job_type = 'k8s_task' ORDER BY id",
    )
    return [{"id": int(row["id"]), "metadata": json.loads(row["metadata"] or "{}")} for row in rows]


def test_pytorchjob_live_captures_master_and_worker(
    k8s_cluster: None,
    glaas_health: str,
    wheel_server: dict[str, str],  # noqa: F811
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    if not _crd_available("pytorchjobs.kubeflow.org"):
        pytest.skip("training-operator v1 not installed; run bootstrap_k8s.sh --with-kubeflow")

    project_dir = tmp_path_factory.mktemp("k8s-ptjob")
    _write_project(project_dir, wheel_server["url"])

    name = f"roar-pt-{uuid.uuid4().hex[:6]}"
    (project_dir / "job.yaml").write_text(
        PYTORCHJOB_TEMPLATE.format(
            name=name,
            namespace=NAMESPACE,
            pinned_node=PINNED_NODE,
            worker_script=_indent_script(PT_WORKER_SCRIPT),
        ),
        encoding="utf-8",
    )

    try:
        completed = _submit("job.yaml", cwd=project_dir)
        logs = _pod_logs(f"training.kubeflow.org/job-name={name}")
        run = {
            "exit_code": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "pod_logs": logs,
        }
        assert completed.returncode == 0, _describe(run)

        tasks = _task_rows(project_dir)
        assert len(tasks) == 2, _describe(run)

        task_names = {str(task["metadata"].get("k8s_task_id") or "") for task in tasks}
        indices = {task_id.split(":")[2] for task_id in task_names if task_id.count(":") >= 3}
        assert indices == {"0", "1"}, (
            f"expected RANK-derived node indices 0/1, got {task_names}\n{_describe(run)}"
        )

        # Per-role task names flow from the replica-spec adapter.
        all_metadata = json.dumps([task["metadata"] for task in tasks])
        assert f"{name}/Master/pytorch" in all_metadata or "Master" in all_metadata
        assert "Worker" in all_metadata, all_metadata
    finally:
        _cleanup(name, resource="pytorchjob")
        import subprocess

        subprocess.run(
            ["docker", "exec", PINNED_NODE, "rm", "-rf", f"/var/tmp/roar-e2e-pt-{name}"],
            check=False,
            capture_output=True,
        )


@pytest.fixture()
def slim_training_runtime() -> Any:
    """Clone the shipped torch-distributed runtime with a slim image."""
    runtime_name = "roar-e2e-slim"
    result = kubectl(
        ["get", "clustertrainingruntime", "torch-distributed", "-o", "json"],
        check=False,
    )
    if result.returncode != 0:
        pytest.skip("trainer v2 runtimes not installed; run bootstrap_k8s.sh --with-kubeflow")

    runtime = json.loads(result.stdout)
    runtime["metadata"] = {"name": runtime_name}
    container = runtime["spec"]["template"]["spec"]["replicatedJobs"][0]["template"]["spec"][
        "template"
    ]["spec"]["containers"][0]
    container["image"] = "python:3.12-slim"

    kubectl(["apply", "-f", "-"], input_text=json.dumps(runtime))
    try:
        yield runtime_name
    finally:
        kubectl(
            ["delete", "clustertrainingruntime", runtime_name, "--ignore-not-found"],
            check=False,
        )


def test_trainjob_live_runs_through_jobset_pipeline(
    k8s_cluster: None,
    glaas_health: str,
    wheel_server: dict[str, str],  # noqa: F811
    slim_training_runtime: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    if not _crd_available("trainjobs.trainer.kubeflow.org"):
        pytest.skip("trainer v2 not installed; run bootstrap_k8s.sh --with-kubeflow")

    project_dir = tmp_path_factory.mktemp("k8s-trainjob")
    _write_project(project_dir, wheel_server["url"])

    name = f"roar-tj-{uuid.uuid4().hex[:6]}"
    (project_dir / "job.yaml").write_text(
        TRAINJOB_TEMPLATE.format(
            name=name,
            namespace=NAMESPACE,
            runtime=slim_training_runtime,
        ),
        encoding="utf-8",
    )

    try:
        completed = _submit("job.yaml", cwd=project_dir)
        logs = _pod_logs(f"jobset.sigs.k8s.io/jobset-name={name}")
        run = {
            "exit_code": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "pod_logs": logs,
        }
        assert completed.returncode == 0, _describe(run)

        tasks = _task_rows(project_dir)
        assert len(tasks) == 2, _describe(run)

        task_ids = {str(task["metadata"].get("k8s_task_id") or "") for task in tasks}
        indices = {task_id.split(":")[2] for task_id in task_ids if task_id.count(":") >= 3}
        assert indices == {"0", "1"}, (
            f"expected completion indices 0/1 across TrainJob nodes, got {task_ids}"
        )
        assert all(":node:" in task_id for task_id in task_ids), task_ids
    finally:
        _cleanup(name, resource="trainjob")
