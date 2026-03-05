"""Ray Data job used to verify worker-side I/O capture."""

from __future__ import annotations

import json
import os

import ray


@ray.remote
def run_pipeline(input_path: str, output_dir: str) -> str:
    dataset = ray.data.read_csv(input_path).repartition(4)
    transformed = dataset.map_batches(
        lambda batch: {"id": batch["id"], "value": batch["value"] * 2},
        batch_format="numpy",
    )
    transformed.write_parquet(output_dir)
    return output_dir


def main() -> None:
    ray.init(address="auto")

    input_path = "/tmp/ray_data_input.csv"
    output_dir = "/tmp/ray_data_output"
    os.makedirs(output_dir, exist_ok=True)

    with open(input_path, "w", encoding="utf-8") as handle:
        handle.write("id,value\n")
        for idx in range(1, 101):
            handle.write(f"{idx},{idx * 3}\n")

    ray.get(run_pipeline.remote(input_path, output_dir))

    print(json.dumps({"input_path": input_path, "output_dir": output_dir}))


if __name__ == "__main__":
    main()
