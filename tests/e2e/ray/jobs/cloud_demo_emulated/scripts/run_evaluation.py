"""Cloud-demo-emulated evaluation phase wrapper."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR.parent) not in sys.path:
    sys.path.insert(0, str(APP_DIR.parent))

from cloud_demo_emulated.workload.evaluation import run_evaluation  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Run emulated evaluation phase")
    parser.add_argument("--state-file", required=True)
    args = parser.parse_args()

    state_path = Path(args.state_file)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    run_id = str(state["run_id"])
    state["metrics_key"] = run_evaluation(str(state["model_key"]), list(state["shard_keys"]), run_id)
    state_path.write_text(json.dumps(state), encoding="utf-8")
    print(f"Saved evaluation state to {state_path} (metrics={state['metrics_key']})")


if __name__ == "__main__":
    main()
