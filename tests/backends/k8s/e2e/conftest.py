"""Fixtures for the Tier-1 KIND k8s lineage smoke tests.

Requires the harness cluster created by
`tests/backends/k8s/scripts/bootstrap_k8s.sh` and a local glaas-api on
http://localhost:3001. Tests skip (with instructions) when either is
missing so the default gate stays green.
"""

from __future__ import annotations

import base64
import json
import shutil
import string
import subprocess
import time
import urllib.parse
import urllib.request
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

HARNESS_DIR = Path(__file__).resolve().parent.parent
MANIFESTS_DIR = HARNESS_DIR / "manifests"
WORKLOADS_DIR = HARNESS_DIR / "workloads"
TOOLS_BIN = HARNESS_DIR / ".tools" / "bin"

CLUSTER_NAME = "roar-k8s-e2e"
KUBE_CONTEXT = f"kind-{CLUSTER_NAME}"
NAMESPACE = "roar-e2e"
HOST_GLAAS_URL = "http://localhost:3001"
JOB_TIMEOUT_SECONDS = 420
BOOTSTRAP_HINT = "run: bash tests/backends/k8s/scripts/bootstrap_k8s.sh"


def _kubectl_bin() -> str | None:
    tool = TOOLS_BIN / "kubectl"
    if tool.is_file():
        return str(tool)
    return shutil.which("kubectl")


def kubectl(
    args: Sequence[str],
    *,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    binary = _kubectl_bin()
    assert binary, f"kubectl not found; {BOOTSTRAP_HINT}"
    result = subprocess.run(
        [binary, "--context", KUBE_CONTEXT, *args],
        input=input_text,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"kubectl {' '.join(args)} failed ({result.returncode}):\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


@pytest.fixture(scope="session")
def k8s_cluster() -> None:
    if _kubectl_bin() is None:
        pytest.skip(f"kubectl not available; {BOOTSTRAP_HINT}")
    result = kubectl(["cluster-info"], check=False)
    if result.returncode != 0:
        pytest.skip(f"KIND cluster {CLUSTER_NAME} not reachable; {BOOTSTRAP_HINT}")
    result = kubectl(["get", "service", "glaas", "-n", NAMESPACE], check=False)
    if result.returncode != 0:
        pytest.skip(f"glaas Service missing in namespace {NAMESPACE}; {BOOTSTRAP_HINT}")


@pytest.fixture(scope="session")
def glaas_health() -> str:
    try:
        with urllib.request.urlopen(f"{HOST_GLAAS_URL}/api/v1/health", timeout=5) as response:
            assert response.status == 200
    except Exception:
        pytest.skip(f"glaas-api not reachable at {HOST_GLAAS_URL}; start it first")
    return HOST_GLAAS_URL


def register_fragment_session_with_glaas() -> dict[str, str]:
    from roar.execution.fragments.sessions import generate_fragment_session
    from roar.integrations.glaas import GlaasClient

    session = generate_fragment_session()
    client = GlaasClient(base_url=HOST_GLAAS_URL, force_anonymous=True)
    result, error = client.register_fragment_session(
        session["session_id"],
        session["token_hash"],
        ttl_seconds=3600,
    )
    assert error is None, f"fragment session registration failed: {error}"
    assert result is not None
    return session


def fetch_fragment_batches(session_id: str, token: str) -> list[dict[str, Any]]:
    encoded_token = urllib.parse.quote(token, safe="")
    url = f"{HOST_GLAAS_URL}/api/v1/fragments/sessions/{session_id}/fragments?token={encoded_token}"
    with urllib.request.urlopen(url, timeout=10) as response:
        payload = json.loads(response.read().decode("utf-8"))
    fragments = payload.get("fragments")
    if fragments is None and isinstance(payload.get("data"), dict):
        fragments = payload["data"].get("fragments")
    assert isinstance(fragments, list), f"Expected fragment batches from {url}, got: {payload!r}"
    return [item for item in fragments if isinstance(item, dict)]


def decrypt_fragment_batches(
    batches: Sequence[dict[str, Any]],
    token: str,
) -> list[dict[str, Any]]:
    key = bytes.fromhex(token)
    decrypted: list[dict[str, Any]] = []
    for batch in batches:
        encrypted_batch = batch.get("encrypted_batch")
        if not isinstance(encrypted_batch, str) or not encrypted_batch:
            continue
        payload = base64.b64decode(encrypted_batch)
        plaintext = AESGCM(key).decrypt(payload[:12], payload[12:], None)
        decoded = json.loads(plaintext.decode("utf-8"))
        if isinstance(decoded, list):
            decrypted.extend(item for item in decoded if isinstance(item, dict))
    return decrypted


def _render_job_manifest(job_name: str, configmap_name: str, secret_name: str) -> str:
    template = string.Template((MANIFESTS_DIR / "job-single.yaml.tpl").read_text(encoding="utf-8"))
    return template.substitute(
        job_name=job_name,
        namespace=NAMESPACE,
        configmap_name=configmap_name,
        secret_name=secret_name,
    )


def _wait_for_job(job_name: str) -> tuple[bool, str]:
    """Poll the Job until it completes or fails, so failures surface fast."""
    deadline = time.time() + JOB_TIMEOUT_SECONDS
    while time.time() < deadline:
        result = kubectl(["get", f"job/{job_name}", "-n", NAMESPACE, "-o", "json"], check=False)
        if result.returncode == 0:
            payload = json.loads(result.stdout)
            for condition in payload.get("status", {}).get("conditions") or []:
                if condition.get("status") != "True":
                    continue
                if condition.get("type") in ("Complete", "SuccessCriteriaMet"):
                    return True, ""
                if condition.get("type") in ("Failed", "FailureTarget"):
                    return False, str(condition.get("message") or "job failed")
        time.sleep(5)
    return False, f"timed out after {JOB_TIMEOUT_SECONDS}s"


def _pod_logs_for_job(job_name: str) -> str:
    result = kubectl(
        ["logs", "-n", NAMESPACE, "-l", f"job-name={job_name}", "--tail=200"],
        check=False,
    )
    return result.stdout + result.stderr


@pytest.fixture(scope="module")
def smoke_run(k8s_cluster: None, glaas_health: str) -> dict[str, Any]:
    """Run the wrapped single-pod training Job once and return its lineage."""
    suffix = uuid.uuid4().hex[:6]
    job_name = f"roar-smoke-{suffix}"
    configmap_name = f"roar-smoke-workload-{suffix}"
    secret_name = f"roar-smoke-session-{suffix}"

    session = register_fragment_session_with_glaas()

    kubectl(
        [
            "create",
            "configmap",
            configmap_name,
            "-n",
            NAMESPACE,
            f"--from-file={WORKLOADS_DIR}",
        ]
    )
    kubectl(
        [
            "create",
            "secret",
            "generic",
            secret_name,
            "-n",
            NAMESPACE,
            f"--from-literal=session_id={session['session_id']}",
            f"--from-literal=token={session['token']}",
        ]
    )

    try:
        manifest = _render_job_manifest(job_name, configmap_name, secret_name)
        kubectl(["apply", "-f", "-"], input_text=manifest)

        succeeded, failure_reason = _wait_for_job(job_name)
        logs = _pod_logs_for_job(job_name)
        assert succeeded, f"Job {job_name} did not complete: {failure_reason}\npod logs:\n{logs}"

        batches = fetch_fragment_batches(session["session_id"], session["token"])
        fragments = decrypt_fragment_batches(batches, session["token"])
        return {
            "job_name": job_name,
            "session": session,
            "batches": batches,
            "fragments": fragments,
            "logs": logs,
        }
    finally:
        for kind_name in (
            f"job/{job_name}",
            f"configmap/{configmap_name}",
            f"secret/{secret_name}",
        ):
            kubectl(["delete", kind_name, "-n", NAMESPACE, "--ignore-not-found"], check=False)
