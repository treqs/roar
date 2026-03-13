"""E2E probe for worker_process_setup_hook failures."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.e2e.ray.conftest import submit_job_on_head

COMPOSE_FILE = Path(__file__).resolve().parent / "docker-compose.yml"
JOBS_DIR = "/app/tests/e2e/ray/jobs"
_SEGFAULT_MARKERS = (
    "sigsegv",
    "segmentation fault",
    "getnamedactorinfo",
)


def _parse_json_line(stdout: str) -> dict[str, Any]:
    for line in reversed(stdout.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return {}


@pytest.mark.e2e
@pytest.mark.ray_e2e
@pytest.mark.ray_diagnostic
def test_worker_process_setup_hook_job_succeeds(ray_cluster: dict[str, str]) -> None:
    del ray_cluster

    stdout, stderr, returncode = submit_job_on_head(
        COMPOSE_FILE,
        f"{JOBS_DIR}/setup_hook_probe.py",
    )
    combined_output = "\n".join(part for part in (stdout, stderr) if part)
    assert returncode == 0, f"Probe runner failed:\n{combined_output}"

    payload = _parse_json_line(stdout)
    assert payload, f"Expected JSON payload in stdout, got:\n{combined_output}"

    status = str(payload.get("status") or "")
    logs = str(payload.get("logs") or "")
    segfault_text = "\n".join(part for part in (combined_output, logs) if part).lower()

    assert status == "SUCCEEDED", (
        "Expected the submitted Ray job to succeed when "
        "`roar.execution.runtime.worker_bootstrap.startup` runs as "
        "`worker_process_setup_hook`.\n"
        f"payload={json.dumps(payload, sort_keys=True)}\n"
        f"stdout:\n{stdout}\n"
        f"stderr:\n{stderr}"
    )
    assert not any(marker in segfault_text for marker in _SEGFAULT_MARKERS), (
        "Expected Ray job output to avoid segfault markers after deferring proxy "
        "endpoint lookup until the first tracked open.\n"
        f"payload={json.dumps(payload, sort_keys=True)}\n"
        f"stdout:\n{stdout}\n"
        f"stderr:\n{stderr}"
    )
