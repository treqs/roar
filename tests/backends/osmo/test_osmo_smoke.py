from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

import pytest

from .conftest import HOST_DOWNLOADS_DIR, osmo_exec, wait_for_workflow_completion

pytestmark = [pytest.mark.e2e, pytest.mark.osmo_e2e]


def test_osmo_basic_workflow_submit_and_complete(osmo_harness: dict[str, str]) -> None:
    workflow_name = f"roar-osmo-basic-{uuid.uuid4().hex[:8]}"
    output_dataset = f"{workflow_name}-output"

    submit = osmo_exec(
        [
            "osmo",
            "workflow",
            "submit",
            "tests/e2e/osmo/workflows/basic.yaml",
            "--pool",
            "default",
            "--set-string",
            f"workflow_name={workflow_name}",
            f"output_dataset={output_dataset}",
            "--format-type",
            "json",
        ],
        timeout=10 * 60,
    )
    submit_payload = json.loads(submit.stdout)
    workflow_id = str(submit_payload["name"])

    wait_for_workflow_completion(workflow_id)

    logs = osmo_exec(
        ["osmo", "workflow", "logs", workflow_id, "--task", "basic"],
        timeout=5 * 60,
    )
    assert "ROAR_OSMO_BASIC_OK" in logs.stdout

    host_download_dir = HOST_DOWNLOADS_DIR / workflow_name
    container_download_dir = Path(osmo_harness["container_downloads_dir"]) / workflow_name
    shutil.rmtree(host_download_dir, ignore_errors=True)
    host_download_dir.mkdir(parents=True, exist_ok=True)

    osmo_exec(
        [
            "osmo",
            "dataset",
            "download",
            f"{output_dataset}:latest",
            str(container_download_dir),
        ],
        timeout=10 * 60,
    )

    result_files = list(host_download_dir.rglob("result.txt"))
    assert result_files, f"expected result.txt under {host_download_dir}"
    assert result_files[0].read_text(encoding="utf-8").strip() == "ROAR_OSMO_BASIC_OK"
