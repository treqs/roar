"""
Ray job for testing per-task file I/O attribution.

Each task writes to a uniquely named file so tests can verify
that roar captured the write AND attributed it to the correct task.
"""

from __future__ import annotations

import json
import os
import sys

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

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(payload, f)

    return payload


@ray.remote
def read_and_summarize(paths: list[str]) -> dict:
    """Read multiple files written by other tasks and return a summary."""
    ctx = ray.get_runtime_context()
    records = []
    for path in paths:
        with open(path, encoding="utf-8") as f:
            records.append(json.load(f))
    return {
        "reader_task_id": ctx.get_task_id(),
        "reader_node_id": ctx.get_node_id(),
        "records_read": len(records),
        "paths": paths,
    }


if __name__ == "__main__":
    output_dir = sys.argv[1] if len(sys.argv) > 1 else "/shared/attributed"
    os.makedirs(output_dir, exist_ok=True)

    ray.init(address="auto")

    # Distribute 6 write tasks across workers
    write_refs = [write_attributed_file.remote(i, output_dir) for i in range(6)]
    write_results = ray.get(write_refs)

    # Read all outputs from a single task
    written_paths = [r["output_path"] for r in write_results]
    summary = ray.get(read_and_summarize.remote(written_paths))

    print(json.dumps({"writes": write_results, "summary": summary}))
