"""Driver phase wrapper for Ray timing contract tests."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from timing_contract.workload import run_phase

PHASE_PRE_SLEEP_SECONDS = 0.7
PHASE_POST_SLEEP_SECONDS = 0.6


def main() -> None:
    parser = argparse.ArgumentParser(description="timing_phase")
    parser.add_argument("--state-file", required=True)
    args = parser.parse_args()

    state_path = Path(args.state_file)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    run_id = str(state["run_id"])

    phase_started_at = time.time()
    time.sleep(PHASE_PRE_SLEEP_SECONDS)
    task_result = run_phase(run_id)
    time.sleep(PHASE_POST_SLEEP_SECONDS)
    phase_ended_at = time.time()

    state.update(
        {
            "artifact_path": task_result["artifact_path"],
            "report_key": task_result["report_key"],
            "phase_started_at": phase_started_at,
            "phase_ended_at": phase_ended_at,
            "phase_expected_duration_seconds": PHASE_PRE_SLEEP_SECONDS
            + float(task_result["expected_duration_seconds"])
            + PHASE_POST_SLEEP_SECONDS,
            "task_started_at": task_result["task_started_at"],
            "task_ended_at": task_result["task_ended_at"],
            "task_expected_duration_seconds": task_result["expected_duration_seconds"],
        }
    )
    state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")


if __name__ == "__main__":
    main()
