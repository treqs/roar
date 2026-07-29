"""Phase-2 e2e: retry chaos and mount-map rewriting.

- Chaos: a Job whose first pod fails mid-run (backoffLimit 1) must yield
  attempt-distinct, non-conflated lineage — both attempts recorded as
  separate k8s_task jobs keyed by pod UID, with the retry succeeding.
- Mount map: a hostPath volume standing in for a FUSE mount, declared via
  ``[k8s.mount_map]`` config, has its file I/O rewritten to object-store
  URIs at reconstitution.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import uuid
from pathlib import Path

import pytest

from .conftest import NAMESPACE
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

FLAKY_SCRIPT = """\
import json
import sys
from pathlib import Path

marker = Path("/state/attempt-1-done")
if not marker.exists():
    marker.write_text("first attempt failed here")
    Path("partial.bin").write_bytes(b"partial-progress")
    print("attempt 1 failing deliberately")
    sys.exit(1)

Path("dataset.csv").write_text("x\\n1\\n2\\n")
rows = Path("dataset.csv").read_text().strip().splitlines()
Path("model.bin").write_bytes(str(len(rows)).encode() * 8)
print("attempt 2 succeeded")
"""

CHAOS_JOB_TEMPLATE = """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: {name}-worker
  namespace: {namespace}
data:
  flaky_train.py: |
{worker_script}
---
apiVersion: batch/v1
kind: Job
metadata:
  name: {name}
  namespace: {namespace}
  labels:
    app.kubernetes.io/part-of: roar-k8s-e2e
spec:
  backoffLimit: 1
  ttlSecondsAfterFinished: 1800
  template:
    spec:
      restartPolicy: Never
      nodeSelector:
        kubernetes.io/hostname: {pinned_node}
      volumes:
        - name: work
          emptyDir: {{}}
        - name: state
          hostPath:
            path: /var/tmp/roar-e2e-state-{name}
            type: DirectoryOrCreate
        - name: workload
          configMap:
            name: {name}-worker
      containers:
        - name: trainer
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
          command: ["python", "/workload/flaky_train.py"]
"""

MOUNT_JOB_TEMPLATE = """\
apiVersion: batch/v1
kind: Job
metadata:
  name: {name}
  namespace: {namespace}
  labels:
    app.kubernetes.io/part-of: roar-k8s-e2e
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 1800
  template:
    spec:
      restartPolicy: Never
      nodeSelector:
        kubernetes.io/hostname: {pinned_node}
      volumes:
        - name: work
          emptyDir: {{}}
        - name: data
          hostPath:
            path: /var/tmp/roar-e2e-mount-{name}
            type: DirectoryOrCreate
      containers:
        - name: trainer
          image: python:3.12-slim
          workingDir: /work
          volumeMounts:
            - name: work
              mountPath: /work
            - name: data
              mountPath: /data
          command:
            - python
            - -c
            - >-
              from pathlib import Path;
              data = Path('/data/input.csv').read_text();
              Path('/data/derived').mkdir(exist_ok=True);
              Path('/data/derived/copy.csv').write_text(data);
              Path('model.bin').write_bytes(data.encode() * 2)
"""


def _query(project_dir: Path, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    conn = sqlite3.connect(project_dir / ".roar" / "roar.db")
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _node_exec(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "exec", PINNED_NODE, *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_pod_retry_produces_attempt_distinct_lineage(
    k8s_cluster: None,
    glaas_health: str,
    wheel_server: dict[str, str],  # noqa: F811
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    project_dir = tmp_path_factory.mktemp("k8s-chaos")
    _write_project(project_dir, wheel_server["url"])

    job_name = f"roar-chaos-{uuid.uuid4().hex[:6]}"
    (project_dir / "job.yaml").write_text(
        CHAOS_JOB_TEMPLATE.format(
            name=job_name,
            namespace=NAMESPACE,
            pinned_node=PINNED_NODE,
            worker_script=_indent_script(FLAKY_SCRIPT),
        ),
        encoding="utf-8",
    )

    state_dir = f"/var/tmp/roar-e2e-state-{job_name}"
    try:
        completed = _submit("job.yaml", cwd=project_dir)
        logs = _pod_logs(f"job-name={job_name}")
        run = {
            "exit_code": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "pod_logs": logs,
        }
        # The retry succeeded, so the submit as a whole succeeds.
        assert completed.returncode == 0, _describe(run)

        tasks = _query(
            project_dir,
            "SELECT id, exit_code, metadata FROM jobs WHERE job_type = 'k8s_task' ORDER BY id",
        )
        assert len(tasks) == 2, (
            f"expected both attempts as distinct tasks, got {len(tasks)}\n{_describe(run)}"
        )

        pod_uids = set()
        for row in tasks:
            metadata = json.loads(row["metadata"] or "{}")
            pod_uids.add(str(metadata.get("k8s_pod_uid") or ""))
        assert len(pod_uids) == 2 and "" not in pod_uids, (
            f"attempts must be keyed by distinct pod UIDs: {pod_uids}\n{_describe(run)}"
        )

        exit_codes = sorted(int(row["exit_code"] or 0) for row in tasks)
        assert exit_codes == [0, 1], (
            f"expected one failed and one successful attempt, got {exit_codes}"
        )

        outputs_by_task: dict[int, set[str]] = {}
        for row in tasks:
            outputs_by_task[int(row["id"])] = {
                str(item["path"])
                for item in _query(
                    project_dir,
                    "SELECT path FROM job_outputs WHERE job_id = ?",
                    (row["id"],),
                )
            }
        all_outputs = set().union(*outputs_by_task.values())
        assert any(path.endswith("partial.bin") for path in all_outputs), all_outputs
        assert any(path.endswith("model.bin") for path in all_outputs), all_outputs
        # The failed attempt's partial output must not be conflated into
        # the successful attempt's job.
        assert not any(
            {p for p in paths if p.endswith("partial.bin")}
            and {p for p in paths if p.endswith("model.bin")}
            for paths in outputs_by_task.values()
        ), outputs_by_task
    finally:
        _cleanup(job_name)
        _node_exec("rm", "-rf", state_dir)


def test_mounted_storage_paths_rewrite_to_object_uris(
    k8s_cluster: None,
    glaas_health: str,
    wheel_server: dict[str, str],  # noqa: F811
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    project_dir = tmp_path_factory.mktemp("k8s-mounts")
    _write_project(project_dir, wheel_server["url"])
    config_path = project_dir / ".roar" / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + '\n[k8s.mount_map]\n"/data" = "s3://mounted-bucket/datasets"\n',
        encoding="utf-8",
    )

    job_name = f"roar-mount-{uuid.uuid4().hex[:6]}"
    (project_dir / "job.yaml").write_text(
        MOUNT_JOB_TEMPLATE.format(
            name=job_name,
            namespace=NAMESPACE,
            pinned_node=PINNED_NODE,
        ),
        encoding="utf-8",
    )

    mount_dir = f"/var/tmp/roar-e2e-mount-{job_name}"
    seeded = _node_exec(
        "sh", "-c", f"mkdir -p {mount_dir} && printf 'x,y\\n1,2\\n' > {mount_dir}/input.csv"
    )
    assert seeded.returncode == 0, seeded.stderr

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

        tasks = _query(project_dir, "SELECT id FROM jobs WHERE job_type = 'k8s_task'")
        assert len(tasks) == 1, _describe(run)
        task_id = tasks[0]["id"]

        input_paths = {
            str(row["path"])
            for row in _query(
                project_dir, "SELECT path FROM job_inputs WHERE job_id = ?", (task_id,)
            )
        }
        output_paths = {
            str(row["path"])
            for row in _query(
                project_dir, "SELECT path FROM job_outputs WHERE job_id = ?", (task_id,)
            )
        }
        assert "s3://mounted-bucket/datasets/input.csv" in input_paths, (
            f"mounted read not rewritten: {input_paths}\n{_describe(run)}"
        )
        assert "s3://mounted-bucket/datasets/derived/copy.csv" in output_paths, (
            f"mounted write not rewritten: {output_paths}\n{_describe(run)}"
        )
        assert any(path.endswith("model.bin") for path in output_paths), output_paths
        # No raw mount paths may leak through.
        assert not any(path.startswith("/data/") for path in input_paths | output_paths)
    finally:
        _cleanup(job_name)
        _node_exec("rm", "-rf", mount_dir)
