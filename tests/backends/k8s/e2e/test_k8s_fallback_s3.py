"""Phase-2 e2e: bundle-mode fallback and in-pod S3 capture.

- Bundle fallback: a pod whose cluster-visible GLaaS URL is a black hole
  writes its fragment bundle to a shared volume (``k8s.bundle_dir``); the
  bundle is pulled off the node and merged with ``roar k8s ingest-bundles``.
- S3 hooks: a pod reads a dataset from MinIO and writes a model back via
  boto3; the object I/O appears in lineage as ``s3://`` refs with etag
  hashes alongside the tracer-captured local files. Requires
  ``bootstrap_k8s.sh --with-minio``.
"""

from __future__ import annotations

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
    _pod_logs,
    _roar,
    _submit,
    _write_project,
)
from .test_k8s_product_path import wheel_server  # noqa: F401  (module fixture reuse)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.k8s_e2e,
    pytest.mark.timeout(900),
]

MINIO_HOST_URL = "http://localhost:39000"
MINIO_CLUSTER_URL = "http://minio:9000"

BUNDLE_JOB_TEMPLATE = """\
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
        - name: bundles
          hostPath:
            path: /var/tmp/roar-e2e-bundles-{name}
            type: DirectoryOrCreate
      containers:
        - name: trainer
          image: python:3.12-slim
          workingDir: /work
          volumeMounts:
            - name: work
              mountPath: /work
            - name: bundles
              mountPath: /bundles
          command:
            - python
            - -c
            - >-
              open('data.csv', 'w').write('x\\n1\\n2\\n');
              rows = open('data.csv').read().strip().splitlines();
              open('model.bin', 'wb').write(str(len(rows)).encode() * 8)
"""

S3_WORKER_SCRIPT = """\
import os

import boto3
from botocore.client import Config

endpoint = os.environ.get("AWS_ENDPOINT_URL", "http://minio:9000")
bucket = os.environ["ROAR_E2E_BUCKET"]
s3 = boto3.client(
    "s3",
    endpoint_url=endpoint,
    config=Config(s3={"addressing_style": "path"}),
)

header = s3.get_object(Bucket=bucket, Key="datasets/train.csv", Range="bytes=0-9")
header["Body"].read()
obj = s3.get_object(Bucket=bucket, Key="datasets/train.csv")
data = obj["Body"].read()
model = data * 3
s3.put_object(Bucket=bucket, Key="models/model.bin", Body=model)
with open("metrics.json", "w") as handle:
    handle.write('{"rows": %d}' % len(data.splitlines()))
print("s3 train done")
"""

S3_JOB_TEMPLATE = """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: {name}-worker
  namespace: {namespace}
data:
  s3_train.py: |
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
  ttlSecondsAfterFinished: 1800
  template:
    spec:
      restartPolicy: Never
      volumes:
        - name: work
          emptyDir: {{}}
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
            - name: workload
              mountPath: /workload
              readOnly: true
          env:
            - name: AWS_ACCESS_KEY_ID
              value: minioadmin
            - name: AWS_SECRET_ACCESS_KEY
              value: minioadmin
            - name: AWS_DEFAULT_REGION
              value: us-east-1
            - name: AWS_ENDPOINT_URL
              value: {minio_cluster_url}
            - name: ROAR_E2E_BUCKET
              value: {bucket}
          command:
            - bash
            - -c
            - pip install --quiet boto3 && python /workload/s3_train.py
"""


def _indent_script(script: str, spaces: int = 4) -> str:
    pad = " " * spaces
    return "\n".join(f"{pad}{line}" if line else "" for line in script.splitlines())


def _query(project_dir: Path, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    conn = sqlite3.connect(project_dir / ".roar" / "roar.db")
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def test_bundle_fallback_when_glaas_unreachable(
    k8s_cluster: None,
    glaas_health: str,
    wheel_server: dict[str, str],  # noqa: F811
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    project_dir = tmp_path_factory.mktemp("k8s-bundle")
    _write_project(project_dir, wheel_server["url"])
    config_path = project_dir / ".roar" / "config.toml"
    config = config_path.read_text(encoding="utf-8").replace(
        'cluster_glaas_url = "http://glaas:3001"',
        'cluster_glaas_url = "http://roar-blackhole.invalid:9"\nbundle_dir = "/bundles"',
    )
    config_path.write_text(config, encoding="utf-8")

    job_name = f"roar-bundle-{uuid.uuid4().hex[:6]}"
    (project_dir / "job.yaml").write_text(
        BUNDLE_JOB_TEMPLATE.format(
            name=job_name,
            namespace=NAMESPACE,
            pinned_node=PINNED_NODE,
        ),
        encoding="utf-8",
    )

    # /var/tmp, not /tmp: KIND nodes mount /tmp as tmpfs, which docker cp
    # cannot read from.
    node_bundle_dir = f"/var/tmp/roar-e2e-bundles-{job_name}"
    try:
        completed = _submit("job.yaml", cwd=project_dir)
        logs = _pod_logs(f"job-name={job_name}")
        run = {
            "exit_code": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "pod_logs": logs,
        }
        # Lineage is best-effort: the unreachable GLaaS must not fail the job.
        assert completed.returncode == 0, _describe(run)
        assert "wrote fragment bundle" in logs, _describe(run)

        # No fragments could stream, so nothing reconstituted yet.
        assert not _query(project_dir, "SELECT id FROM jobs WHERE job_type = 'k8s_task'")

        # Pull the bundle off the node the way an operator would pull it
        # off a PVC, then ingest it.
        copied = tmp_path_factory.mktemp("bundles-copy")
        subprocess.run(
            ["docker", "cp", f"{PINNED_NODE}:{node_bundle_dir}/.", str(copied)],
            check=True,
            capture_output=True,
        )
        ingested = _roar(["k8s", "ingest-bundles", str(copied)], cwd=project_dir)
        assert ingested.returncode == 0, ingested.stdout + ingested.stderr
        assert "1 fragment(s) merged" in ingested.stdout, ingested.stdout

        tasks = _query(project_dir, "SELECT id FROM jobs WHERE job_type = 'k8s_task'")
        assert len(tasks) == 1
        outputs = {
            str(row["path"])
            for row in _query(
                project_dir,
                "SELECT path FROM job_outputs WHERE job_id = ?",
                (tasks[0]["id"],),
            )
        }
        assert any(path.endswith("model.bin") for path in outputs), outputs
    finally:
        _cleanup(job_name)
        subprocess.run(
            ["docker", "exec", PINNED_NODE, "rm", "-rf", node_bundle_dir],
            check=False,
            capture_output=True,
        )


def _minio_available() -> bool:
    result = kubectl(["get", "deployment/minio", "-n", NAMESPACE], check=False)
    return result.returncode == 0


def test_s3_object_io_captured_in_lineage(
    k8s_cluster: None,
    glaas_health: str,
    wheel_server: dict[str, str],  # noqa: F811
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    if not _minio_available():
        pytest.skip("MinIO not deployed; run bootstrap_k8s.sh --with-minio")

    import boto3
    from botocore.client import Config

    suffix = uuid.uuid4().hex[:6]
    bucket = f"roar-e2e-{suffix}"
    dataset_body = b"x,y\n1.0,2.0\n2.0,3.9\n3.0,6.1\n"

    # Host-visible endpoint: stage the dataset the way a data pipeline
    # already would have (also exercises the host/cluster endpoint split).
    host_s3 = boto3.client(
        "s3",
        endpoint_url=MINIO_HOST_URL,
        aws_access_key_id="minioadmin",
        aws_secret_access_key="minioadmin",
        region_name="us-east-1",
        config=Config(s3={"addressing_style": "path"}),
    )
    host_s3.create_bucket(Bucket=bucket)
    host_s3.put_object(Bucket=bucket, Key="datasets/train.csv", Body=dataset_body)

    project_dir = tmp_path_factory.mktemp("k8s-s3")
    _write_project(project_dir, wheel_server["url"])

    job_name = f"roar-s3-{suffix}"
    (project_dir / "job.yaml").write_text(
        S3_JOB_TEMPLATE.format(
            name=job_name,
            namespace=NAMESPACE,
            worker_script=_indent_script(S3_WORKER_SCRIPT),
            minio_cluster_url=MINIO_CLUSTER_URL,
            bucket=bucket,
        ),
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

        tasks = _query(project_dir, "SELECT id FROM jobs WHERE job_type = 'k8s_task'")
        assert len(tasks) == 1, _describe(run)
        task_id = tasks[0]["id"]

        input_paths = {
            str(row["path"])
            for row in _query(
                project_dir,
                "SELECT path FROM job_inputs WHERE job_id = ?",
                (task_id,),
            )
        }
        output_paths = {
            str(row["path"])
            for row in _query(
                project_dir,
                "SELECT path FROM job_outputs WHERE job_id = ?",
                (task_id,),
            )
        }
        assert f"s3://{bucket}/datasets/train.csv" in input_paths, (
            f"S3 read missing: {input_paths}\n{_describe(run)}"
        )
        assert f"s3://{bucket}/models/model.bin" in output_paths, (
            f"S3 write missing: {output_paths}\n{_describe(run)}"
        )
        assert any(path.endswith("metrics.json") for path in output_paths), output_paths

        etag_rows = _query(
            project_dir,
            "SELECT COUNT(*) AS count FROM artifact_hashes WHERE algorithm = 'etag'",
        )
        assert int(etag_rows[0]["count"]) >= 2, "expected etag hashes for the S3 artifacts"

        ranged = _query(
            project_dir,
            "SELECT byte_ranges FROM job_inputs WHERE path = ? AND job_id = ?",
            (f"s3://{bucket}/datasets/train.csv", task_id),
        )
        assert ranged and ranged[0]["byte_ranges"] == "[[0,9]]", (
            f"ranged read not captured: {[dict(row) for row in ranged]}\n{_describe(run)}"
        )
    finally:
        _cleanup(job_name)
