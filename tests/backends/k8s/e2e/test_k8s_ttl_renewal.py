"""Fragment-session TTL renewal e2e: training that outlives its session.

Registers the fragment session with the minimum TTL (60s) and runs a Job
that finishes after the session has expired. Without renewal the pod's
fragment POSTs 403 and the finalizer's fetch 403s too — lineage is lost.
With renewal (streamer renew-on-403 + the glaas-api renew endpoint) the
run reconstitutes normally.

Skips when the local glaas-api does not expose the renewal endpoint yet.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

import pytest

from .conftest import HOST_GLAAS_URL, KUBE_CONTEXT, NAMESPACE, TOOLS_BIN, kubectl
from .test_k8s_product_path import wheel_server  # noqa: F401  (module fixture reuse)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.k8s_e2e,
    # The job deliberately sleeps past the 60s session TTL.
    pytest.mark.timeout(900),
]

SLEEP_SECONDS = 75

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
            - python
            - -c
            - >-
              import time;
              time.sleep({sleep_seconds});
              open('model.bin', 'wb').write(b'slow-training' * 4)
"""


def _glaas_supports_renewal() -> bool:
    token = secrets.token_bytes(32).hex()
    session_id = str(uuid.uuid4())
    register = urllib.request.Request(
        url=f"{HOST_GLAAS_URL}/api/v1/fragments/sessions",
        data=json.dumps(
            {
                "session_id": session_id,
                "token_hash": hashlib.sha256(token.encode("utf-8")).hexdigest(),
                "ttl_seconds": 60,
            }
        ).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    renew = urllib.request.Request(
        url=f"{HOST_GLAAS_URL}/api/v1/fragments/sessions/{session_id}/renew",
        data=json.dumps({"ttl_seconds": 60}).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "x-roar-fragment-token": token,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(register, timeout=5):
            pass
        with urllib.request.urlopen(renew, timeout=5) as response:
            return response.status == 200
    except urllib.error.HTTPError:
        return False
    except Exception:
        return False


@pytest.fixture(scope="module")
def renewal_run(
    k8s_cluster: None,
    glaas_health: str,
    wheel_server: dict[str, str],  # noqa: F811
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, Any]:
    if not _glaas_supports_renewal():
        pytest.skip("local glaas-api lacks fragment-session renewal (POST .../renew)")

    project_dir = tmp_path_factory.mktemp("k8s-ttl-renewal")
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
                "fragment_session_ttl_seconds = 60",
                "wait_for_completion = true",
                "wait_timeout_seconds = 420",
                "poll_interval_seconds = 2.0",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    job_name = f"roar-ttl-renew-{uuid.uuid4().hex[:6]}"
    manifest_path = project_dir / "job.yaml"
    manifest_path.write_text(
        JOB_MANIFEST_TEMPLATE.format(
            job_name=job_name,
            namespace=NAMESPACE,
            sleep_seconds=SLEEP_SECONDS,
        ),
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
            timeout=800,
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


def test_lineage_survives_session_expiry(renewal_run: dict[str, Any]) -> None:
    """The 60s session expires mid-run; renewal keeps stream + fetch working."""
    assert renewal_run["exit_code"] == 0, _describe(renewal_run)
    combined = renewal_run["stdout"] + renewal_run["stderr"]
    assert "lineage reconstituted" in combined, _describe(renewal_run)

    db_path = Path(renewal_run["project_dir"]) / ".roar" / "roar.db"
    assert db_path.is_file(), _describe(renewal_run)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        tasks = conn.execute("SELECT id FROM jobs WHERE job_type = 'k8s_task'").fetchall()
        assert len(tasks) == 1, _describe(renewal_run)
        outputs = conn.execute(
            "SELECT path FROM job_outputs WHERE job_id = ?",
            (tasks[0]["id"],),
        ).fetchall()
    finally:
        conn.close()
    assert any(str(row["path"]).endswith("model.bin") for row in outputs), _describe(renewal_run)
