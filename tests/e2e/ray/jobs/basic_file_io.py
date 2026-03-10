"""Ray job for basic file I/O across remote tasks."""

from __future__ import annotations

import json
from pathlib import Path

import ray


@ray.remote
def run_file_io_pipeline(input_path: str, output_path: str) -> dict[str, object]:
    seed_payload = {"a": 1, "b": 2, "label": "sample"}
    Path(input_path).parent.mkdir(parents=True, exist_ok=True)
    with open(input_path, "w", encoding="utf-8") as handle:
        json.dump(seed_payload, handle)
    with open(input_path, encoding="utf-8") as handle:
        payload = json.load(handle)

    transformed = {
        key: (
            value * 2 if isinstance(value, (int, float)) and not isinstance(value, bool) else value
        )
        for key, value in payload.items()
    }

    with open(output_path, "w", encoding="utf-8") as handle:
        json.dump(transformed, handle)

    with open(output_path, encoding="utf-8") as handle:
        result = json.load(handle)
    return {"input_path": input_path, "output_path": output_path, "result": result}


def main() -> None:
    ray.init(address="auto")
    base_dir = Path.cwd() / "artifacts" / "basic_file_io"
    result = ray.get(
        run_file_io_pipeline.remote(
            str(base_dir / "input.json"),
            str(base_dir / "output.json"),
        )
    )
    print(json.dumps(result))


if __name__ == "__main__":
    main()
