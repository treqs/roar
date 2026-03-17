from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import textwrap
import time
import urllib.error
import urllib.request
import uuid

import pytest

from .conftest import (
    REPO_ROOT,
    osmo_exec,
    osmo_port_forward,
    wait_for_workflow_status,
)

pytestmark = [pytest.mark.e2e, pytest.mark.osmo_e2e]

RAY_DASHBOARD_PORT = 18265


def _host_ray_cli() -> str:
    candidates = [
        REPO_ROOT / ".venv" / "bin" / "ray",
        REPO_ROOT.parent / "roar" / ".venv" / "bin" / "ray",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    ray_cli = shutil.which("ray")
    if ray_cli:
        return ray_cli
    pytest.skip("ray CLI is not available on the host")


def _host_ray_version(ray_cli: str) -> str:
    result = subprocess.run(
        [ray_cli, "--version"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    match = re.search(r"version\s+([0-9.]+)", result.stdout)
    if not match:
        raise AssertionError(f"could not parse Ray version from: {result.stdout!r}")
    return match.group(1)


def _wait_for_dashboard(url: str) -> dict[str, object]:
    deadline = time.monotonic() + 5 * 60
    last_error = ""
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/api/version", timeout=10) as response:
                payload = json.load(response)
            if payload.get("ray_version"):
                return payload
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            last_error = str(exc)
        time.sleep(2)
    raise AssertionError(f"timed out waiting for Ray dashboard at {url}: {last_error}")


def _wait_for_task_log_marker(workflow_id: str, task: str, marker: str) -> str:
    deadline = time.monotonic() + 10 * 60
    last_output = ""
    while time.monotonic() < deadline:
        try:
            result = osmo_exec(
                ["osmo", "workflow", "logs", workflow_id, "--task", task],
                timeout=10,
                check=False,
            )
            output = f"{result.stdout}\n{result.stderr}"
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode()
            stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode()
            output = f"{stdout}\n{stderr}"
        if marker in output:
            return output
        if output.strip():
            last_output = output
        time.sleep(5)
    raise AssertionError(
        f"timed out waiting for {marker!r} in logs for {workflow_id}/{task}.\n"
        f"last_output:\n{textwrap.indent(last_output, '  ')}"
    )


def test_osmo_ray_cluster_accepts_host_submitted_ray_job(
    osmo_harness: dict[str, str],
) -> None:
    del osmo_harness
    ray_cli = _host_ray_cli()
    ray_version = _host_ray_version(ray_cli)
    workflow_name = f"roar-osmo-ray-{uuid.uuid4().hex[:8]}"

    submit = osmo_exec(
        [
            "osmo",
            "workflow",
            "submit",
            "tests/backends/osmo/workflows/ray_cluster.yaml",
            "--pool",
            "default",
            "--set-string",
            f"workflow_name={workflow_name}",
            f"ray_version={ray_version}",
            "--format-type",
            "json",
        ],
        timeout=10 * 60,
    )
    workflow_id = str(json.loads(submit.stdout)["name"])

    try:
        wait_for_workflow_status(workflow_id, lambda status: status == "RUNNING")
        _wait_for_task_log_marker(workflow_id, "master", "ROAR_OSMO_RAY_HEAD_READY")

        with osmo_port_forward(
            workflow_id,
            "master",
            local_port=RAY_DASHBOARD_PORT,
            task_port=8265,
        ) as forwarded:
            dashboard_payload = _wait_for_dashboard(forwarded["url"])
            assert dashboard_payload.get("ray_version") == ray_version

            result = subprocess.run(
                [
                    ray_cli,
                    "job",
                    "submit",
                    "--address",
                    forwarded["url"],
                    "--log-style",
                    "record",
                    "--log-color",
                    "false",
                    "--working-dir",
                    "tests/backends/osmo/workloads",
                    "--",
                    "python",
                    "ray_smoke_job.py",
                ],
                cwd=REPO_ROOT,
                env={**dict(os.environ), "ROAR_EXPECTED_RAY_NODES": "1"},
                capture_output=True,
                text=True,
                timeout=10 * 60,
            )

            submit_output = f"{result.stdout}\n{result.stderr}"
            job_id_match = re.search(r"Job '([^']+)'", submit_output)
            assert job_id_match is not None, submit_output
            job_id = job_id_match.group(1)

            logs_result = subprocess.run(
                [
                    ray_cli,
                    "job",
                    "logs",
                    "--address",
                    forwarded["url"],
                    "--log-style",
                    "record",
                    "--log-color",
                    "false",
                    job_id,
                ],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                timeout=5 * 60,
            )

        logs_output = f"{logs_result.stdout}\n{logs_result.stderr}"
        output = f"{submit_output}\n{logs_result.stdout}\n{logs_result.stderr}"
        assert result.returncode == 0, (
            f"ray job submit failed (rc={result.returncode}).\n"
            f"stdout/stderr:\n{textwrap.indent(submit_output, '  ')}"
        )
        assert logs_result.returncode == 0, (
            f"ray job logs failed (rc={logs_result.returncode}).\n"
            f"stdout/stderr:\n{textwrap.indent(logs_output, '  ')}"
        )
        marker_line = next(
            (line for line in output.splitlines() if "ROAR_OSMO_RAY_OK " in line),
            None,
        )
        assert marker_line is not None, output
        payload = json.loads(marker_line.split("ROAR_OSMO_RAY_OK ", 1)[1])
        assert payload["node_count"] >= 1, output
        assert payload["total"] == 55, output
    finally:
        osmo_exec(
            ["osmo", "workflow", "cancel", workflow_id],
            timeout=5 * 60,
            check=False,
        )
