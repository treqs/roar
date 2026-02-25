"""Ray job for basic file I/O across remote tasks."""

from __future__ import annotations

import json

import ray


@ray.remote
def write_file(path: str, data: str) -> str:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(data)
    return path


@ray.remote
def read_file(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


@ray.remote
def transform(input_path: str, output_path: str) -> dict[str, object]:
    with open(input_path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)

    transformed = {
        key: (value * 2 if isinstance(value, (int, float)) and not isinstance(value, bool) else value)
        for key, value in payload.items()
    }

    with open(output_path, "w", encoding="utf-8") as handle:
        json.dump(transformed, handle)

    return transformed


def main() -> None:
    ray.init(address="auto")
    input_path = "/shared/input.json"
    output_path = "/shared/output.json"

    seed_payload = {"a": 1, "b": 2, "label": "sample"}
    ray.get(write_file.remote(input_path, json.dumps(seed_payload)))
    ray.get(transform.remote(input_path, output_path))

    result = json.loads(ray.get(read_file.remote(output_path)))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
