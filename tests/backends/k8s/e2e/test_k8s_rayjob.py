"""Live RayJob delegation smoke on KubeRay.

Requires ``bootstrap_k8s.sh --with-kuberay``; skips when the CRD is
absent. The k8s backend rewrites the RayJob (entrypoint through the Ray
driver entrypoint, runtime env with the roar wheel + Ray env contract,
Secret refs into the cluster pods) and delegates reconstitution to the
Ray backend — merged lineage lands as ``ray_task`` jobs under the k8s
submit node.

This is the heaviest e2e in the harness (a multi-GB Ray image plus a
per-job pip env); everything pins to one node so the image pulls once.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import uuid
from pathlib import Path

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
    pytest.mark.timeout(1500),
]

# Defaults to the native compose harness's pin
# (tests/backends/ray/e2e/Dockerfile); override with ROAR_E2E_RAY_IMAGE to
# smoke other Ray versions (verified: 2.46.0 and 2.54.0).
RAY_IMAGE = os.environ.get("ROAR_E2E_RAY_IMAGE", "rayproject/ray:2.54.0-py312-cpu")

RAY_TRAIN_SCRIPT = """\
from pathlib import Path

import ray

ray.init()


@ray.remote
def train_shard() -> int:
    Path("/home/ray/task-out.bin").write_bytes(b"trained" * 4)
    return 1


assert ray.get(train_shard.remote()) == 1
Path("/home/ray/driver-out.txt").write_text("done")
print("ray train done")
"""

RAYJOB_TEMPLATE = """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: {name}-worker
  namespace: {namespace}
data:
  ray_train.py: |
{worker_script}
---
apiVersion: ray.io/v1
kind: RayJob
metadata:
  name: {name}
  namespace: {namespace}
  labels:
    app.kubernetes.io/part-of: roar-k8s-e2e
spec:
  entrypoint: python /workload/ray_train.py
  shutdownAfterJobFinishes: true
  ttlSecondsAfterFinished: 1800
  submitterPodTemplate:
    spec:
      restartPolicy: Never
      nodeSelector:
        kubernetes.io/hostname: {pinned_node}
      containers:
        - name: ray-job-submitter
          image: {ray_image}
  rayClusterSpec:
    rayVersion: "{ray_version}"
    headGroupSpec:
      rayStartParams:
        num-cpus: "1"
      template:
        spec:
          nodeSelector:
            kubernetes.io/hostname: {pinned_node}
          volumes:
            - name: workload
              configMap:
                name: {name}-worker
          containers:
            - name: ray-head
              image: {ray_image}
              volumeMounts:
                - name: workload
                  mountPath: /workload
                  readOnly: true
              resources:
                requests:
                  cpu: 500m
                  memory: 1Gi
    workerGroupSpecs:
      - groupName: workers
        replicas: 1
        rayStartParams:
          num-cpus: "1"
        template:
          spec:
            nodeSelector:
              kubernetes.io/hostname: {pinned_node}
            volumes:
              - name: workload
                configMap:
                  name: {name}-worker
            containers:
              - name: ray-worker
                image: {ray_image}
                volumeMounts:
                  - name: workload
                    mountPath: /workload
                    readOnly: true
                resources:
                  requests:
                    cpu: 500m
                    memory: 1Gi
"""


def _query(project_dir: Path, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    conn = sqlite3.connect(project_dir / ".roar" / "roar.db")
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def test_rayjob_live_delegates_to_ray_backend(
    k8s_cluster: None,
    glaas_health: str,
    wheel_server: dict[str, str],  # noqa: F811
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    if kubectl(["get", "crd", "rayjobs.ray.io"], check=False).returncode != 0:
        pytest.skip("KubeRay not installed; run bootstrap_k8s.sh --with-kuberay")

    # One node, one pull: the Ray image is multi-GB.
    pull = subprocess.run(
        ["docker", "exec", PINNED_NODE, "crictl", "pull", f"docker.io/{RAY_IMAGE}"],
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    assert pull.returncode == 0, f"ray image pre-pull failed: {pull.stderr}"

    project_dir = tmp_path_factory.mktemp("k8s-rayjob")
    _write_project(project_dir, wheel_server["url"])
    config_path = project_dir / ".roar" / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "wait_timeout_seconds = 420",
            "wait_timeout_seconds = 900",
        ),
        encoding="utf-8",
    )

    name = f"roar-ray-{uuid.uuid4().hex[:6]}"
    (project_dir / "rayjob.yaml").write_text(
        RAYJOB_TEMPLATE.format(
            name=name,
            namespace=NAMESPACE,
            pinned_node=PINNED_NODE,
            ray_image=RAY_IMAGE,
            worker_script=_indent_script(RAY_TRAIN_SCRIPT),
            ray_version=RAY_IMAGE.split(":")[1].split("-")[0],
        ),
        encoding="utf-8",
    )

    try:
        completed = _submit("rayjob.yaml", cwd=project_dir, timeout=1200)
        logs = _pod_logs(f"ray.io/originated-from-cr-name={name}")
        run = {
            "exit_code": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "pod_logs": logs,
        }
        assert completed.returncode == 0, _describe(run)

        submit_rows = _query(
            project_dir,
            "SELECT job_uid FROM jobs WHERE execution_backend = 'k8s' "
            "AND execution_role = 'submit'",
        )
        assert len(submit_rows) == 1, _describe(run)

        ray_tasks = _query(
            project_dir,
            "SELECT id, parent_job_uid FROM jobs WHERE job_type = 'ray_task'",
        )
        assert ray_tasks, f"expected ray_task jobs from delegated reconstitution\n{_describe(run)}"

        # Every ray task must carry the DAG edge back to the recorded k8s
        # submit job (regression: driver_job_uid was not threaded into the
        # worker env, so tasks merged parentless and no test noticed).
        submit_uid = str(submit_rows[0]["job_uid"])
        orphaned = [
            row["id"] for row in ray_tasks if str(row["parent_job_uid"] or "") != submit_uid
        ]
        assert not orphaned, (
            f"ray_task jobs not parented to submit job {submit_uid}: {orphaned}\n{_describe(run)}"
        )

        output_paths = {
            str(row["path"])
            for row in _query(
                project_dir,
                "SELECT o.path FROM job_outputs o JOIN jobs j ON j.id = o.job_id "
                "WHERE j.job_type = 'ray_task'",
            )
        }
        assert any(path.endswith("task-out.bin") for path in output_paths), (
            f"ray task file write missing from lineage: {output_paths}\n{_describe(run)}"
        )
    finally:
        _cleanup(name, resource="rayjob")
