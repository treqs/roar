"""Ray job implementing a simple multi-step ETL pipeline."""

from __future__ import annotations

import pandas as pd

import ray


@ray.remote
def extract(input_path: str) -> list[dict[str, object]]:
    frame = pd.read_csv(input_path)
    return frame.to_dict(orient="records")


@ray.remote
def transform(records: list[dict[str, object]]) -> list[dict[str, object]]:
    transformed: list[dict[str, object]] = []
    for record in records:
        updated = dict(record)
        value = updated.get("value")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            updated["value"] = value * 2
        transformed.append(updated)
    return transformed


@ray.remote
def load(records: list[dict[str, object]], output_path: str) -> str:
    frame = pd.DataFrame.from_records(records)
    frame.to_parquet(output_path, index=False)
    return output_path


def main() -> None:
    ray.init(address="auto")

    input_path = "/tmp/pipeline_input.csv"
    output_path = "/tmp/pipeline_output.parquet"

    pd.DataFrame(
        [
            {"id": 1, "value": 10},
            {"id": 2, "value": 20},
            {"id": 3, "value": 30},
        ]
    ).to_csv(input_path, index=False)

    records = ray.get(extract.remote(input_path))
    transformed = ray.get(transform.remote(records))
    result = ray.get(load.remote(transformed, output_path))

    print(result)


if __name__ == "__main__":
    main()
