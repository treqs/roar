"""Ray job implementing a simple multi-step ETL pipeline."""

from __future__ import annotations

import pandas as pd
from pathlib import Path

import ray


@ray.remote
def run_pipeline(base_dir: str) -> dict[str, str]:
    base_path = Path(base_dir)
    base_path.mkdir(parents=True, exist_ok=True)
    input_path = base_path / "pipeline_input.csv"
    output_path = base_path / "pipeline_output.parquet"

    pd.DataFrame(
        [
            {"id": 1, "value": 10},
            {"id": 2, "value": 20},
            {"id": 3, "value": 30},
        ]
    ).to_csv(input_path, index=False)

    frame = pd.read_csv(input_path)
    transformed: list[dict[str, object]] = []
    for record in frame.to_dict(orient="records"):
        updated = dict(record)
        value = updated.get("value")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            updated["value"] = value * 2
        transformed.append(updated)
    frame = pd.DataFrame.from_records(transformed)
    frame.to_parquet(output_path, index=False)
    return {"input_path": str(input_path), "output_path": str(output_path)}


def main() -> None:
    ray.init(address="auto")
    result = ray.get(run_pipeline.remote(str(Path.cwd() / "artifacts" / "pipeline")))
    print(result["output_path"])


if __name__ == "__main__":
    main()
