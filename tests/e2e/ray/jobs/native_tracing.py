"""Ray job that verifies native preload env wiring in workers."""

from __future__ import annotations

import json
import os
from pathlib import Path

import ray


@ray.remote
def write_and_report(path: str) -> dict[str, str]:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("native tracing smoke\n")
    return {
        "path": path,
        "ld_preload": os.environ.get("LD_PRELOAD", ""),
        "aws_endpoint_url": os.environ.get("AWS_ENDPOINT_URL", ""),
    }


def main() -> None:
    ray.init(address="auto")
    payload = ray.get(
        write_and_report.remote(str(Path.cwd() / "artifacts" / "native_tracing_output.txt"))
    )
    print(json.dumps(payload))


if __name__ == "__main__":
    main()
