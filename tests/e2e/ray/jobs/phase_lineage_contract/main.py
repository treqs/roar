"""Three-phase Ray pipeline for lineage contract coverage."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
PHASE_SCRIPTS = [
    ("extraction", APP_DIR / "scripts" / "run_extraction.py"),
    ("training", APP_DIR / "scripts" / "run_training.py"),
    ("evaluation", APP_DIR / "scripts" / "run_evaluation.py"),
]


def _run_phase(phase: str, script_path: Path, state_file: Path) -> float:
    started = time.perf_counter()
    subprocess.run(
        [sys.executable, str(script_path), "--state-file", str(state_file)],
        check=True,
        cwd=APP_DIR,
    )
    elapsed_ms = (time.perf_counter() - started) * 1000
    print(f"[phase:{phase}] {elapsed_ms:.1f}ms")
    return elapsed_ms


def main() -> None:
    parser = argparse.ArgumentParser(description="phase-lineage-contract")
    parser.add_argument("--run-id", default=None)
    args = parser.parse_args()

    run_id = args.run_id or f"run-{uuid.uuid4().hex[:8]}"
    state_file = Path(f"/tmp/phase-lineage-contract-{run_id}.json")
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({"run_id": run_id}), encoding="utf-8")

    phase_times: dict[str, float] = {}
    for phase, script in PHASE_SCRIPTS:
        phase_times[phase] = _run_phase(phase, script, state_file)

    final_state = json.loads(state_file.read_text(encoding="utf-8"))
    print(
        json.dumps(
            {
                "script": "phase_lineage_contract",
                "run_id": run_id,
                "processed_key": final_state.get("processed_key"),
                "model_key": final_state.get("model_key"),
                "report_key": final_state.get("report_key"),
                "phase_times_ms": phase_times,
            }
        )
    )


if __name__ == "__main__":
    main()
