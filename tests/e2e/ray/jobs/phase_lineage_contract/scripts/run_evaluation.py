"""Phase-lineage evaluation phase wrapper."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR.parent) not in sys.path:
    sys.path.insert(0, str(APP_DIR.parent))

from phase_lineage_contract.workload import run_evaluation


def main() -> None:
    parser = argparse.ArgumentParser(description="Run phase-lineage evaluation")
    parser.add_argument("--state-file", required=True)
    args = parser.parse_args()

    state_path = Path(args.state_file)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    run_id = str(state["run_id"])
    model_key = str(state["model_key"])
    state["report_key"] = run_evaluation(model_key, run_id)
    state_path.write_text(json.dumps(state), encoding="utf-8")
    print(f"Saved evaluation state to {state_path}")


if __name__ == "__main__":
    main()

