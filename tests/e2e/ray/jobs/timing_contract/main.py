"""Host-submit Ray timing contract entrypoint."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
PHASE_SCRIPT = APP_DIR / "scripts" / "run_timing_phase.py"


def main() -> None:
    parser = argparse.ArgumentParser(description="timing_contract")
    parser.add_argument("--run-id", default=None)
    args = parser.parse_args()

    run_id = args.run_id or f"run-{uuid.uuid4().hex[:8]}"
    state_file = Path(f"/tmp/timing-contract-state-{run_id}.json")
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({"run_id": run_id}), encoding="utf-8")

    subprocess.run(
        [sys.executable, str(PHASE_SCRIPT), "--state-file", str(state_file)],
        check=True,
        cwd=APP_DIR,
    )

    final_state = json.loads(state_file.read_text(encoding="utf-8"))
    print(
        json.dumps(
            {
                "script": "timing_contract",
                "run_id": run_id,
                "artifact_path": final_state.get("artifact_path"),
                "report_key": final_state.get("report_key"),
                "phase_started_at": final_state.get("phase_started_at"),
                "phase_ended_at": final_state.get("phase_ended_at"),
                "phase_expected_duration_seconds": final_state.get(
                    "phase_expected_duration_seconds"
                ),
                "task_started_at": final_state.get("task_started_at"),
                "task_ended_at": final_state.get("task_ended_at"),
                "task_expected_duration_seconds": final_state.get("task_expected_duration_seconds"),
            }
        )
    )


if __name__ == "__main__":
    main()
