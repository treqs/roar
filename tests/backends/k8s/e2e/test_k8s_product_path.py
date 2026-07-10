"""Product-path e2e: `roar run kubectl apply -f job.yaml` on the KIND harness.

Exercises the real `roar.backends.k8s` backend end to end: manifest
rewriting at plan time, Secret-delivered fragment credentials, in-pod
runtime bootstrap from a host-served wheel, fragment streaming to the
local glaas-api, and shared-finalizer reconstitution into the
submitting project's `.roar/roar.db`.
"""

from __future__ import annotations

import functools
import http.server
import os
import sqlite3
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Any

import pytest

from .conftest import HARNESS_DIR, KUBE_CONTEXT, NAMESPACE, TOOLS_BIN, kubectl

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.k8s_e2e,
    # The module fixture runs a full instrumented Job (wheel download +
    # pip install + trace) inside the first test's budget.
    pytest.mark.timeout(900),
]

REPO_ROOT = HARNESS_DIR.parent.parent.parent
DIST_DIR = REPO_ROOT / "dist"

JOB_MANIFEST_TEMPLATE = """\
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
        - name: trainer
          image: python:3.12-slim
          workingDir: /work
          volumeMounts:
            - name: work
              mountPath: /work
          command:
            - bash
            - -c
            - >-
              printf 'x,y\\n1.0,2.0\\n2.0,3.9\\n3.0,6.1\\n' > dataset.csv &&
              python -c "import hashlib, json;
              rows = open('dataset.csv').read().strip().splitlines();
              open('model.bin', 'wb').write(hashlib.blake2b(str(len(rows)).encode()).digest() * 4);
              json.dump({{'rows': len(rows) - 1}}, open('metrics.json', 'w'))"
"""


def _kind_gateway_ip() -> str:
    result = subprocess.run(
        [
            "docker",
            "network",
            "inspect",
            "kind",
            "-f",
            "{{range .IPAM.Config}}{{.Gateway}} {{end}}",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    for token in result.stdout.split():
        if token.count(".") == 3:
            return token
    raise RuntimeError(f"no IPv4 gateway found on the kind docker network: {result.stdout!r}")


@pytest.fixture(scope="module")
def wheel_server() -> Any:
    wheels = sorted(DIST_DIR.glob("roar_cli-*.whl"))
    if not wheels:
        pytest.skip(f"no roar_cli wheel in {DIST_DIR}; run scripts/build_wheel_with_bins.sh")

    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler,
        directory=str(DIST_DIR),
    )
    server = http.server.ThreadingHTTPServer(("0.0.0.0", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {
            "url": f"http://{_kind_gateway_ip()}:{server.server_address[1]}/{wheels[-1].name}",
        }
    finally:
        server.shutdown()


@pytest.fixture(scope="module")
def product_run(
    k8s_cluster: None,
    glaas_health: str,
    wheel_server: dict[str, str],
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, Any]:
    """Run the full product path once and return project + output state."""
    project_dir = tmp_path_factory.mktemp("k8s-product")
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

    job_name = f"roar-product-{uuid.uuid4().hex[:6]}"
    manifest_path = project_dir / "job.yaml"
    manifest_path.write_text(
        JOB_MANIFEST_TEMPLATE.format(job_name=job_name, namespace=NAMESPACE),
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
            "manifest_path": manifest_path,
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


def test_product_run_succeeds_and_reconstitutes(product_run: dict[str, Any]) -> None:
    assert product_run["exit_code"] == 0, _describe(product_run)
    combined = product_run["stdout"] + product_run["stderr"]
    assert "lineage reconstituted" in combined, _describe(product_run)


def test_submit_job_recorded_with_original_command(product_run: dict[str, Any]) -> None:
    rows = _query(
        product_run,
        "SELECT command, execution_backend, execution_role, exit_code FROM jobs "
        "WHERE execution_backend = 'k8s' AND execution_role = 'submit'",
    )
    assert len(rows) == 1, _describe(product_run)
    submit = rows[0]
    assert submit["exit_code"] == 0
    # The recorded command must be the user's original submit (reproduce
    # re-enters the backend through it), not the rewritten prepared path.
    assert "-f job.yaml" in submit["command"]
    assert ".roar/k8s/prepared" not in submit["command"]


def test_pod_lineage_merged_as_k8s_task(product_run: dict[str, Any]) -> None:
    rows = _query(
        product_run,
        "SELECT id, job_uid, command FROM jobs WHERE job_type = 'k8s_task'",
    )
    assert len(rows) == 1, _describe(product_run)
    task = rows[0]
    assert task["command"].startswith("k8s_task:")

    output_paths = {
        str(row["path"])
        for row in _query(
            product_run,
            "SELECT path FROM job_outputs WHERE job_id = ?",
            (task["id"],),
        )
    }
    assert any(path.endswith("model.bin") for path in output_paths), output_paths
    assert any(path.endswith("metrics.json") for path in output_paths), output_paths

    input_paths = {
        str(row["path"])
        for row in _query(
            product_run,
            "SELECT path FROM job_inputs WHERE job_id = ?",
            (task["id"],),
        )
    }
    assert any(path.endswith("dataset.csv") for path in input_paths), input_paths


def test_prepared_manifest_with_secret_is_cleaned_up(product_run: dict[str, Any]) -> None:
    prepared_dir = Path(product_run["project_dir"]) / ".roar" / "k8s" / "prepared"
    leftovers = list(prepared_dir.glob("*.yaml")) if prepared_dir.is_dir() else []
    assert not leftovers, (
        f"prepared manifests (which embed the token Secret) left behind: {leftovers}"
    )
