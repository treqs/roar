"""Phase-2 e2e: multi-pod distributed capture, attach, and JobSet.

Runs against the KIND harness through the real product path
(`roar run kubectl apply -f ...`). Covers:

- Indexed Job with two pods sharing one fragment session: completion-index
  identity, child-process capture, and a cross-pod artifact edge through a
  shared volume.
- `roar k8s attach` from a fresh project (cluster-Secret credential path)
  after a no-wait submit.
- JobSet through the same worker (skipped unless the JobSet controller is
  installed; `bootstrap_k8s.sh --with-jobset`).
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from .conftest import KUBE_CONTEXT, NAMESPACE, TOOLS_BIN, kubectl
from .test_k8s_product_path import wheel_server  # noqa: F401  (module fixture reuse)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.k8s_e2e,
    pytest.mark.timeout(900),
]

# Both pods pin to one worker node so a hostPath volume can model the
# shared filesystem (RWX PVCs aren't available on the default KIND
# storage class); the cross-pod edge itself is content-hash based.
PINNED_NODE = "roar-k8s-e2e-worker"

WORKER_SCRIPT = """\
import json
import os
import subprocess
import sys
import time
from pathlib import Path

index = os.environ.get("JOB_COMPLETION_INDEX", "0")
shared = Path("/shared")
if index == "0":
    subprocess.run(
        [sys.executable, "-c", "open('child-stats.json', 'w').write('loss 0.1')"],
        check=True,
    )
    weights = b"weights:" + b"0" * 64
    tmp = shared / "weights.bin.tmp"
    tmp.write_bytes(weights)
    tmp.rename(shared / "weights.bin")
    Path("rank0.json").write_text(json.dumps({"rank": 0}))
else:
    deadline = time.time() + 180
    while not (shared / "weights.bin").exists():
        if time.time() > deadline:
            raise SystemExit("timed out waiting for rank 0 weights")
        time.sleep(2)
    data = (shared / "weights.bin").read_bytes()
    Path("eval.json").write_text(json.dumps({"rank": 1, "bytes": len(data)}))
print(f"worker {index} done")
"""

INDEXED_JOB_TEMPLATE = """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: {name}-worker
  namespace: {namespace}
data:
  worker.py: |
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
  backoffLimit: 0
  completions: 2
  parallelism: 2
  completionMode: Indexed
  ttlSecondsAfterFinished: 1800
  template:
    spec:
      restartPolicy: Never
      nodeSelector:
        kubernetes.io/hostname: {pinned_node}
      volumes:
        - name: work
          emptyDir: {{}}
        - name: shared
          hostPath:
            path: /tmp/roar-e2e-shared-{name}
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
            - name: shared
              mountPath: /shared
            - name: workload
              mountPath: /workload
              readOnly: true
          command: ["python", "/workload/worker.py"]
"""

JOBSET_TEMPLATE = """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: {name}-worker
  namespace: {namespace}
data:
  worker.py: |
{worker_script}
---
apiVersion: jobset.x-k8s.io/v1alpha2
kind: JobSet
metadata:
  name: {name}
  namespace: {namespace}
  labels:
    app.kubernetes.io/part-of: roar-k8s-e2e
spec:
  replicatedJobs:
    - name: workers
      replicas: 1
      template:
        spec:
          backoffLimit: 0
          completions: 2
          parallelism: 2
          completionMode: Indexed
          template:
            spec:
              restartPolicy: Never
              nodeSelector:
                kubernetes.io/hostname: {pinned_node}
              volumes:
                - name: work
                  emptyDir: {{}}
                - name: shared
                  hostPath:
                    path: /tmp/roar-e2e-shared-{name}
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
                    - name: shared
                      mountPath: /shared
                    - name: workload
                      mountPath: /workload
                      readOnly: true
                  command: ["python", "/workload/worker.py"]
"""

SINGLE_JOB_TEMPLATE = """\
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
      volumes:
        - name: work
          emptyDir: {{}}
      containers:
        - name: trainer
          image: python:3.12-slim
          workingDir: /work
          volumeMounts:
            - name: work
              mountPath: /work
          command:
            - python
            - -c
            - >-
              import json;
              open('data.csv', 'w').write('x\\n1\\n2\\n');
              rows = open('data.csv').read().strip().splitlines();
              json.dump({{'rows': len(rows) - 1}}, open('summary.json', 'w'))
"""


def _indent_script(script: str, spaces: int = 4) -> str:
    pad = " " * spaces
    return "\n".join(f"{pad}{line}" if line else "" for line in script.splitlines())


def _write_project(project_dir: Path, wheel_url: str, *, wait: bool = True) -> None:
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
                f'runtime_install_requirement = "{wheel_url}"',
                'cluster_glaas_url = "http://glaas:3001"',
                f"wait_for_completion = {'true' if wait else 'false'}",
                "wait_timeout_seconds = 420",
                "poll_interval_seconds = 2.0",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _roar_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PATH"] = f"{TOOLS_BIN}{os.pathsep}{env.get('PATH', '')}"
    env.pop("GLAAS_URL", None)
    env.pop("ROAR_PROJECT_DIR", None)
    return env


def _roar(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "roar", *args],
        cwd=cwd,
        env=_roar_env(),
        capture_output=True,
        text=True,
        check=False,
        timeout=700,
    )


def _submit(manifest_name: str, *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return _roar(
        ["run", "kubectl", "apply", "--context", KUBE_CONTEXT, "-f", manifest_name],
        cwd=cwd,
    )


def _query(project_dir: Path, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    conn = sqlite3.connect(project_dir / ".roar" / "roar.db")
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _pod_logs(selector: str) -> str:
    result = kubectl(
        ["logs", "-n", NAMESPACE, "-l", selector, "--tail=100", "--prefix"],
        check=False,
    )
    return result.stdout + result.stderr


def _cleanup(job_name: str, *, resource: str = "job") -> None:
    kubectl(
        ["delete", f"{resource}/{job_name}", "-n", NAMESPACE, "--ignore-not-found"],
        check=False,
    )
    kubectl(
        ["delete", f"configmap/{job_name}-worker", "-n", NAMESPACE, "--ignore-not-found"],
        check=False,
    )
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


@pytest.fixture(scope="module")
def indexed_run(
    k8s_cluster: None,
    glaas_health: str,
    wheel_server: dict[str, str],  # noqa: F811
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, Any]:
    project_dir = tmp_path_factory.mktemp("k8s-indexed")
    _write_project(project_dir, wheel_server["url"])

    job_name = f"roar-dist-{uuid.uuid4().hex[:6]}"
    (project_dir / "job.yaml").write_text(
        INDEXED_JOB_TEMPLATE.format(
            name=job_name,
            namespace=NAMESPACE,
            pinned_node=PINNED_NODE,
            worker_script=_indent_script(WORKER_SCRIPT),
        ),
        encoding="utf-8",
    )

    try:
        completed = _submit("job.yaml", cwd=project_dir)
        return {
            "project_dir": project_dir,
            "job_name": job_name,
            "exit_code": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "pod_logs": _pod_logs(f"job-name={job_name}"),
        }
    finally:
        _cleanup(job_name)


def _describe(run: dict[str, Any]) -> str:
    return (
        f"exit={run['exit_code']}\nstdout:\n{run['stdout']}\nstderr:\n{run['stderr']}\n"
        f"pod logs:\n{run.get('pod_logs', '')}"
    )


def test_indexed_job_captures_both_pods(indexed_run: dict[str, Any]) -> None:
    assert indexed_run["exit_code"] == 0, _describe(indexed_run)

    tasks = _query(
        indexed_run["project_dir"],
        "SELECT id, metadata FROM jobs WHERE job_type = 'k8s_task' ORDER BY id",
    )
    assert len(tasks) == 2, _describe(indexed_run)

    all_metadata = " ".join(str(row["metadata"] or "") for row in tasks)
    assert ":trainer:0:0" in all_metadata, all_metadata
    assert ":trainer:1:0" in all_metadata, all_metadata


def test_child_process_io_is_captured(indexed_run: dict[str, Any]) -> None:
    outputs = _query(
        indexed_run["project_dir"],
        "SELECT o.path FROM job_outputs o JOIN jobs j ON j.id = o.job_id "
        "WHERE j.job_type = 'k8s_task'",
    )
    paths = {str(row["path"]) for row in outputs}
    assert any(path.endswith("child-stats.json") for path in paths), (
        f"child process write missing from lineage: {paths}\n{_describe(indexed_run)}"
    )


def test_cross_pod_artifact_edge_connects(indexed_run: dict[str, Any]) -> None:
    rows = _query(
        indexed_run["project_dir"],
        "SELECT o.artifact_id AS writer_artifact, i.artifact_id AS reader_artifact, "
        "o.job_id AS writer_job, i.job_id AS reader_job "
        "FROM job_outputs o JOIN job_inputs i ON i.artifact_id = o.artifact_id "
        "WHERE o.path LIKE '%weights.bin' AND i.path LIKE '%weights.bin' "
        "AND o.job_id != i.job_id",
    )
    assert rows, (
        "no cross-pod edge: weights.bin written by rank 0 was not linked to "
        f"rank 1's read\n{_describe(indexed_run)}"
    )

    steps = _query(
        indexed_run["project_dir"],
        "SELECT id, step_number FROM jobs WHERE id IN (?, ?)",
        (rows[0]["writer_job"], rows[0]["reader_job"]),
    )
    step_by_id = {row["id"]: row["step_number"] for row in steps}
    assert step_by_id[rows[0]["reader_job"]] > step_by_id[rows[0]["writer_job"]], (
        f"reader should be downstream of writer: {step_by_id}"
    )


def test_attach_from_fresh_project_via_cluster_secret(
    k8s_cluster: None,
    glaas_health: str,
    wheel_server: dict[str, str],  # noqa: F811
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    submit_dir = tmp_path_factory.mktemp("k8s-submit")
    _write_project(submit_dir, wheel_server["url"], wait=False)

    job_name = f"roar-attach-{uuid.uuid4().hex[:6]}"
    (submit_dir / "job.yaml").write_text(
        SINGLE_JOB_TEMPLATE.format(name=job_name, namespace=NAMESPACE),
        encoding="utf-8",
    )

    try:
        submitted = _submit("job.yaml", cwd=submit_dir)
        assert submitted.returncode == 0, submitted.stdout + submitted.stderr

        # The no-wait submit returns immediately; wait for the job out of band
        # like CI would.
        deadline = time.time() + 300
        while time.time() < deadline:
            status = kubectl(
                ["get", f"job/{job_name}", "-n", NAMESPACE, "-o", "json"],
                check=False,
            )
            if status.returncode == 0:
                conditions = json.loads(status.stdout).get("status", {}).get("conditions") or []
                if any(
                    c.get("status") == "True" and c.get("type") in ("Complete", "Failed")
                    for c in conditions
                ):
                    break
            time.sleep(3)

        attach_dir = tmp_path_factory.mktemp("k8s-attach")
        _write_project(attach_dir, wheel_server["url"])
        attached = _roar(
            [
                "k8s",
                "attach",
                f"job/{job_name}",
                "-n",
                NAMESPACE,
                "--context",
                KUBE_CONTEXT,
            ],
            cwd=attach_dir,
        )
        logs = _pod_logs(f"job-name={job_name}")
        assert attached.returncode == 0, (
            f"attach failed:\n{attached.stdout}\n{attached.stderr}\npod logs:\n{logs}"
        )
        assert "lineage reconstituted" in attached.stdout, attached.stdout

        tasks = _query(
            attach_dir,
            "SELECT j.id FROM jobs j WHERE j.job_type = 'k8s_task'",
        )
        assert len(tasks) == 1, f"{attached.stdout}\n{attached.stderr}\npod logs:\n{logs}"

        attach_jobs = _query(
            attach_dir,
            "SELECT job_uid, execution_role FROM jobs "
            "WHERE execution_backend = 'k8s' AND execution_role = 'attach'",
        )
        assert len(attach_jobs) == 1
        parent_uid = attach_jobs[0]["job_uid"]

        linked = _query(
            attach_dir,
            "SELECT metadata FROM jobs WHERE job_type = 'k8s_task' AND metadata LIKE ?",
            (f"%{parent_uid}%",),
        )
        assert linked, "k8s_task should reference the attach job's parent uid"
    finally:
        _cleanup(job_name)


def _jobset_controller_available() -> bool:
    result = kubectl(["get", "crd", "jobsets.jobset.x-k8s.io"], check=False)
    return result.returncode == 0


def test_jobset_workload_captures_both_pods(
    k8s_cluster: None,
    glaas_health: str,
    wheel_server: dict[str, str],  # noqa: F811
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    if not _jobset_controller_available():
        pytest.skip("JobSet controller not installed; run bootstrap_k8s.sh --with-jobset")

    project_dir = tmp_path_factory.mktemp("k8s-jobset")
    _write_project(project_dir, wheel_server["url"])

    name = f"roar-js-{uuid.uuid4().hex[:6]}"
    (project_dir / "jobset.yaml").write_text(
        JOBSET_TEMPLATE.format(
            name=name,
            namespace=NAMESPACE,
            pinned_node=PINNED_NODE,
            worker_script=_indent_script(WORKER_SCRIPT),
        ),
        encoding="utf-8",
    )

    try:
        completed = _submit("jobset.yaml", cwd=project_dir)
        logs = _pod_logs(f"jobset.sigs.k8s.io/jobset-name={name}")
        assert completed.returncode == 0, (
            f"{completed.stdout}\n{completed.stderr}\npod logs:\n{logs}"
        )

        tasks = _query(
            project_dir,
            "SELECT metadata FROM jobs WHERE job_type = 'k8s_task'",
        )
        assert len(tasks) == 2, f"{completed.stdout}\n{completed.stderr}\npod logs:\n{logs}"
        all_metadata = " ".join(str(row["metadata"] or "") for row in tasks)
        assert ":trainer:0:0" in all_metadata
        assert ":trainer:1:0" in all_metadata
    finally:
        _cleanup(name, resource="jobset")
