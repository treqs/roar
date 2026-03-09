"""Ray job that reports worker native-tracing activation and writes a local file."""

from __future__ import annotations

import json
import os
from pathlib import Path

import ray


def _to_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.hex()
    return str(value)


@ray.remote
def write_and_report(path: str) -> dict[str, str]:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("native tracing smoke\n")
    ctx = ray.get_runtime_context()
    return {
        "path": path,
        "ld_preload": os.environ.get("LD_PRELOAD", ""),
        "trace_sock": os.environ.get("ROAR_PRELOAD_TRACE_SOCK", ""),
        "aws_endpoint_url": os.environ.get("AWS_ENDPOINT_URL", ""),
        "worker_id": _to_text(ctx.get_worker_id()),
        "task_id": _to_text(ctx.get_task_id()),
    }


def main() -> None:
    ray.init(address="auto")
    payload = ray.get(
        write_and_report.remote(str(Path.cwd() / "artifacts" / "native_tracing_output.txt"))
    )
    print(json.dumps(payload))


if __name__ == "__main__":
    main()
