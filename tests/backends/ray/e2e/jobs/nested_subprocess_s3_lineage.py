"""Nested subprocess Ray job for task-scoped S3 lineage coverage."""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path


def main() -> None:
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    app_dir = Path(__file__).resolve().parent
    worker_script = app_dir / "nested_subprocess_s3_worker.py"
    result = subprocess.run(
        [sys.executable, str(worker_script), "--run-id", run_id],
        check=True,
        capture_output=True,
        text=True,
        cwd=app_dir,
    )
    payload = {}
    for line in reversed(result.stdout.splitlines()):
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        payload = json.loads(stripped)
        break
    print(json.dumps({"run_id": run_id, **payload}, sort_keys=True))


if __name__ == "__main__":
    main()
