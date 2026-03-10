"""Ray job that probes the active worker bootstrap path under host submit."""

from __future__ import annotations

import json
import os
from pathlib import Path

import ray


@ray.remote
def _probe(output_path: str) -> dict[str, str]:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("worker bootstrap probe\n", encoding="utf-8")
    with output.open(encoding="utf-8") as handle:
        body = handle.read()

    return {
        "aws_endpoint_url": os.environ.get("AWS_ENDPOINT_URL", ""),
        "body": body,
        "output_path": str(output),
        "roar_project_dir": os.environ.get("ROAR_PROJECT_DIR", ""),
    }


def main() -> None:
    ray.init(address="auto")
    base_dir = Path.cwd() / "artifacts" / "worker_bootstrap_probe"
    payload = ray.get(_probe.remote(str(base_dir / "output.txt")))
    print(json.dumps(payload, sort_keys=True))


if __name__ == "__main__":
    main()
