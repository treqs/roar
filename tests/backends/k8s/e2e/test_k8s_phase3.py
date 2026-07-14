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
        [
            "docker",
            "exec",
            "roar-k8s-e2e-worker",
            "crictl",
            "inspecti",
            "-q",
            f"docker.io/library/{RUNTIME_IMAGE}",
        ],
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
            kubectl(["get", f"job/{job_name}", "-n", NAMESPACE, "-o", "json"]).stdout
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


PROXY_JOB_TEMPLATE = """\
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
          env:
            - name: AWS_ACCESS_KEY_ID
              value: minioadmin
            - name: AWS_SECRET_ACCESS_KEY
              value: minioadmin
            - name: AWS_DEFAULT_REGION
              value: us-east-1
          command:
            - python
            - -c
            - >-
              import urllib.request;
              data = urllib.request.urlopen('http://127.0.0.1:19191/{bucket}/datasets/train.csv', timeout=30).read();
              open('model.bin', 'wb').write(data * 2)
"""


def test_proxy_sidecar_captures_hook_invisible_s3_client(
    k8s_cluster: None,
    glaas_health: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A raw-HTTP S3 client (no boto3 — the hooks are blind to it) reads
    through the injected proxy sidecar, which re-signs the request with the
    workload's inherited AWS credentials; the proxy log lands in lineage."""
    if not _runtime_image_loaded():
        pytest.skip("roar-runtime:dev not loaded")
    if kubectl(["get", "deployment/minio", "-n", NAMESPACE], check=False).returncode != 0:
        pytest.skip("MinIO not deployed; run bootstrap_k8s.sh --with-minio")

    import boto3
    from botocore.client import Config

    suffix = uuid.uuid4().hex[:6]
    bucket = f"roar-proxy-{suffix}"
    host_s3 = boto3.client(
        "s3",
        endpoint_url="http://localhost:39000",
        aws_access_key_id="minioadmin",
        aws_secret_access_key="minioadmin",
        region_name="us-east-1",
        config=Config(s3={"addressing_style": "path"}),
    )
    host_s3.create_bucket(Bucket=bucket)
    host_s3.put_object(Bucket=bucket, Key="datasets/train.csv", Body=b"x,y\n1.0,2.0\n2.0,3.9\n")
    project_dir = tmp_path_factory.mktemp("k8s-proxy-mode")
    _write_image_mode_project(project_dir)
    config_path = project_dir / ".roar" / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + 'proxy_sidecar = true\nproxy_upstream = "http://minio:9000"\n',
        encoding="utf-8",
    )

    job_name = f"roar-proxy-{suffix}"
    (project_dir / "job.yaml").write_text(
        PROXY_JOB_TEMPLATE.format(name=job_name, namespace=NAMESPACE, bucket=bucket),
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
        assert f"s3://{bucket}/datasets/train.csv" in input_paths, (
            f"proxy-captured read missing: {input_paths}\n{_describe(run)}"
        )
        output_paths = {
            str(row["path"])
            for row in _query(
                project_dir,
                "SELECT path FROM job_outputs WHERE job_id = ?",
                (task_id,),
            )
        }
        assert any(path.endswith("model.bin") for path in output_paths), output_paths
    finally:
        _cleanup(job_name)


def _webhook_deployed() -> bool:
    result = kubectl(["get", "mutatingwebhookconfiguration", "roar-lineage-injector"], check=False)
    return result.returncode == 0


def _wait_job_terminal(job_name: str, namespace: str, timeout: int = 300) -> bool:
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        status = kubectl(["get", f"job/{job_name}", "-n", namespace, "-o", "json"], check=False)
        if status.returncode == 0:
            conditions = json.loads(status.stdout).get("status", {}).get("conditions") or []
            if any(
                c.get("status") == "True" and c.get("type") in ("Complete", "Failed")
                for c in conditions
            ):
                return any(
                    c.get("status") == "True" and c.get("type") == "Complete" for c in conditions
                )
        import time as _time

        _time.sleep(3)
    return False


def test_webhook_injects_lineage_zero_touch(
    k8s_cluster: None,
    glaas_health: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """The full zero-touch story: plain kubectl apply, no roar on the client.

    A labeled namespace gets automatic injection; lineage is recovered
    afterwards with `roar k8s attach` using the webhook-created Secret.
    """
    if not _webhook_deployed():
        pytest.skip("webhook not deployed; run bootstrap_k8s.sh --with-webhook")
    if not _runtime_image_loaded():
        pytest.skip("roar-runtime:dev not loaded")

    from .test_k8s_distributed import _roar

    suffix = uuid.uuid4().hex[:6]
    auto_ns = f"roar-e2e-auto-{suffix}"
    job_name = f"roar-auto-{suffix}"

    kubectl(["create", "namespace", auto_ns])
    kubectl(["label", "namespace", auto_ns, "roar.glaas.ai/lineage=enabled"])
    try:
        # Plain kubectl apply — the client knows nothing about roar.
        manifest = SINGLE_JOB_TEMPLATE.format(name=job_name, namespace=auto_ns)
        kubectl(["apply", "-f", "-"], input_text=manifest)

        job_doc = json.loads(
            kubectl(["get", f"job/{job_name}", "-n", auto_ns, "-o", "json"]).stdout
        )
        annotations = job_doc["metadata"].get("annotations") or {}
        parent_uid = annotations.get("roar.glaas.ai/parent-uid")
        assert parent_uid, f"webhook did not annotate the Job: {annotations}"
        pod_spec = job_doc["spec"]["template"]["spec"]
        assert "roar-runtime-staging" in [c["name"] for c in pod_spec.get("initContainers", [])]
        assert "roar.backends.k8s.pod_entry" in pod_spec["containers"][0]["command"][2]

        assert _wait_job_terminal(job_name, auto_ns), (
            f"job did not complete:\n{_pod_logs(f'job-name={job_name}')}"
        )

        attach_dir = tmp_path_factory.mktemp("k8s-webhook-attach")
        _write_image_mode_project(attach_dir)
        attached = _roar(
            ["k8s", "attach", f"job/{job_name}", "-n", auto_ns, "--context", KUBE_CONTEXT],
            cwd=attach_dir,
        )
        assert attached.returncode == 0, attached.stdout + attached.stderr
        assert "lineage reconstituted" in attached.stdout, attached.stdout

        tasks = _query(attach_dir, "SELECT id FROM jobs WHERE job_type = 'k8s_task'")
        assert len(tasks) == 1
        attach_rows = _query(
            attach_dir,
            "SELECT job_uid FROM jobs WHERE execution_role = 'attach'",
        )
        assert attach_rows and attach_rows[0]["job_uid"] == parent_uid
    finally:
        kubectl(["delete", "namespace", auto_ns, "--ignore-not-found"], check=False)


def test_webhook_leaves_unlabeled_namespaces_untouched(
    k8s_cluster: None,
) -> None:
    if not _webhook_deployed():
        pytest.skip("webhook not deployed; run bootstrap_k8s.sh --with-webhook")

    job_name = f"roar-plain-{uuid.uuid4().hex[:6]}"
    try:
        manifest = SINGLE_JOB_TEMPLATE.format(name=job_name, namespace=NAMESPACE)
        kubectl(["apply", "-f", "-"], input_text=manifest)
        job_doc = json.loads(
            kubectl(["get", f"job/{job_name}", "-n", NAMESPACE, "-o", "json"]).stdout
        )
        annotations = job_doc["metadata"].get("annotations") or {}
        assert "roar.glaas.ai/parent-uid" not in annotations
        command = job_doc["spec"]["template"]["spec"]["containers"][0]["command"]
        assert "roar.backends.k8s.pod_entry" not in " ".join(command)
    finally:
        kubectl(["delete", f"job/{job_name}", "-n", NAMESPACE, "--ignore-not-found"], check=False)
