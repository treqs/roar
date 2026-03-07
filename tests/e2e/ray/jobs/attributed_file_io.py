"""
Ray job for testing per-task file I/O attribution.

Each task writes to a uniquely named file so tests can verify
that roar captured the write AND attributed it to the correct task.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import ray


@ray.remote
def write_attributed_file(task_index: int, output_dir: str) -> dict:
    """Write a file and return metadata about this task's execution."""
    ctx = ray.get_runtime_context()
    task_id = ctx.get_task_id()
    node_id = ctx.get_node_id()

    output_path = os.path.join(output_dir, f"task_{task_index:03d}_output.json")
    payload = {
        "task_index": task_index,
        "task_id": task_id,
        "node_id": node_id,
        "output_path": output_path,
    }

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)

    return payload


if __name__ == "__main__":
    default_dir = Path.cwd() / "artifacts" / "attributed"
    output_dir = sys.argv[1] if len(sys.argv) > 1 else str(default_dir)
    os.makedirs(output_dir, exist_ok=True)

    ray.init(address="auto")

    write_refs = [write_attributed_file.remote(i, output_dir) for i in range(6)]
    write_results = ray.get(write_refs)
    print(json.dumps({"writes": write_results}))
